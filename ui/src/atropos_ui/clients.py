"""Network adapters; URLs retain VM cluster tenant / reverse proxy prefixes."""
import csv
import json
import math
import os
import time
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from atropos_ui.models import Datasource, MetricDefinition, validate_url
from atropos_ui.resource_limits import limit
from atropos_ui.service_api import DatasourceHealth


class ApiError(RuntimeError):
    pass


METRIC_SEARCH_MIN_LENGTH = 3
METRIC_SEARCH_LIMIT = 200


def metric_name_pattern(search: str) -> str:
    search = search.strip()
    if len(search) < METRIC_SEARCH_MIN_LENGTH:
        raise ValueError(f'Введите минимум {METRIC_SEARCH_MIN_LENGTH} символа имени метрики.')
    escaped = ''.join('\\' + char if char in r'\.^$|?*+(){}[]' else char for char in search)
    return f'(?i).*{escaped}.*'


class HttpApi:
    def __init__(self, url: str, token_env: str = "", *, transport=None, datasource: Datasource | None = None):
        self.url = validate_url(url)
        loopback_host = os.getenv('ATROPOS_SOURCE_LOOPBACK_HOST')
        if datasource is not None and loopback_host:
            parsed = urlsplit(self.url)
            if parsed.hostname in {'localhost', '127.0.0.1', '::1'}:
                host = loopback_host + (':' + str(parsed.port) if parsed.port else '')
                self.url = validate_url(urlunsplit((parsed.scheme, host, parsed.path, '', '')))
        self.token_env = token_env
        self.transport = transport
        self.datasource = datasource

    def request(self, method: str, path: str, *, raw=False, max_response_bytes=None, **kwargs):
        budget = max_response_bytes if max_response_bytes is not None else limit('HTTP_MAX_BYTES', 64 * 1024 * 1024)
        headers = dict(kwargs.pop("headers", {}))
        source = self.datasource
        token_env = source.token_env if source else self.token_env
        if token_env:
            token = os.getenv(token_env)
            if not token:
                raise ApiError(f"Не задана переменная окружения {token_env}.")
            scheme = "Token" if source and source.kind == "InfluxDB" and source.influx_version == "2" else "Bearer"
            headers["Authorization"] = f"{scheme} {token}"
        auth = None
        if source and source.auth_type == "basic":
            password = os.getenv(source.password_env)
            if password is None:
                raise ApiError(f"Не задана переменная окружения {source.password_env}.")
            auth = httpx.BasicAuth(source.username, password)
        timeout = source.timeout_seconds if source else 20
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(5, timeout)), transport=self.transport,
                              follow_redirects=False, verify=source.tls_verify if source else True) as client:
                with client.stream(method, self.url + path, headers=headers, auth=auth, **kwargs) as response:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if len(content) + len(chunk) > budget:
                            raise ApiError('Ответ источника превышает лимит ATROPOS_HTTP_MAX_BYTES. '
                                           'Сузьте запрос или настройте лимит.')
                        content.extend(chunk)
                    return content.decode(response.encoding or 'utf-8') if raw else json.loads(content)
        except httpx.HTTPStatusError as exc:
            raise ApiError(f"{path}: HTTP {exc.response.status_code}. Проверьте адрес, доступ и запрос.") from exc
        except (httpx.HTTPError, ValueError) as exc:
            # Do not echo response bodies / request headers containing credentials.
            raise ApiError(f"{path}: сервер недоступен либо вернул некорректный ответ.") from exc

    def prometheus(self, path: str, params: dict) -> dict | list:
        body = self.request("GET", path, params=params)
        if not isinstance(body, dict) or body.get("status") != "success" or "data" not in body:
            raise ApiError("Prometheus API отклонил запрос или вернул ответ неизвестного формата.")
        return body["data"]


def validate_window(start: float, end: float, step: int):
    if not all(math.isfinite(x) for x in (start, end, step)) or end <= start or step <= 0:
        raise ValueError("Конец периода должен быть позже начала, шаг — положительным.")
    if (end - start) / step > 11000:
        raise ValueError("Более 11000 точек на ряд. Увеличьте шаг или сократите период.")


class PrometheusClient(HttpApi):
    def names(self, search: str) -> list[str]:
        pattern = metric_name_pattern(search)
        result = self.prometheus("/api/v1/label/__name__/values", {
            'match[]': '{__name__=~' + json.dumps(pattern) + '}',
            'limit': METRIC_SEARCH_LIMIT + 1,
        })
        if not isinstance(result, list) or not all(isinstance(x, str) for x in result):
            raise ApiError("Ожидался список имён метрик.")
        return result[:METRIC_SEARCH_LIMIT + 1]

    def series(self, metric: str, start: float, end: float) -> list[dict]:
        from atropos_ui.models import selector
        result = self.prometheus("/api/v1/series", {"match[]": selector(metric), "start": start, "end": end})
        if not isinstance(result, list) or not all(isinstance(x, dict) for x in result):
            raise ApiError("Ожидался список рядов.")
        return result

    def instant(self, query: str) -> dict:
        if not query.strip():
            raise ValueError("Введите PromQL.")
        return self.prometheus("/api/v1/query", {"query": query})

    def range(self, query: str, start: float, end: float, step: int) -> dict:
        validate_window(start, end, step)
        if self.datasource and step < self.datasource.min_interval_seconds:
            raise ValueError(f"Шаг должен быть не меньше {self.datasource.min_interval_seconds} секунд для этого источника.")
        if not query.strip():
            raise ValueError("Введите PromQL.")
        return self.prometheus("/api/v1/query_range", {"query": query, "start": start, "end": end, "step": step})


def check_datasource(source: Datasource, *, transport=None) -> DatasourceHealth:
    """Read-only backend-specific probes, separate from datasource configuration."""
    api = HttpApi(source.url, datasource=source, transport=transport)
    try:
        if source.prometheus_compatible:
            data = api.prometheus("/api/v1/query", {"query": "vector(1)"})
            try:
                valid = (isinstance(data, dict) and data.get("resultType") == "vector"
                         and isinstance(data.get("result"), list) and len(data["result"]) == 1
                         and len(data["result"][0]["value"]) == 2
                         and math.isfinite(float(data["result"][0]["value"][0]))
                         and float(data["result"][0]["value"][1]) == 1)
            except (KeyError, TypeError, ValueError, OverflowError):
                valid = False
            if not valid:
                raise ApiError("Ожидался instant-vector от Prometheus API.")
            message = "Подключение доступно. Prometheus API выполнил проверочный запрос."
        elif source.kind == "ClickHouse":
            data = api.request("GET", "/", params={
                "database": source.database, "query": "SELECT 1 AS value FORMAT JSON", "readonly": "2",
                "max_execution_time": max(1, int(source.timeout_seconds)),
            })
            if not isinstance(data, dict) or data.get("data") != [{"value": 1}]:
                raise ApiError("ClickHouse вернул некорректный ответ на SELECT 1.")
            message = "Подключение доступно. ClickHouse выполнил SELECT 1 в выбранной базе."
        elif source.influx_version == "1":
            data = api.request("GET", "/query", params={"db": source.database, "q": "SHOW MEASUREMENTS LIMIT 1"})
            if (not isinstance(data, dict) or "error" in data or not isinstance(data.get("results"), list)
                    or not data["results"] or any(not isinstance(row, dict) or "error" in row for row in data["results"])):
                raise ApiError("InfluxDB отклонил проверку указанной базы данных.")
            message = "Подключение доступно. InfluxQL проверил доступ к указанной базе."
        elif source.influx_version == "2":
            # Reading at most one record per returned Flux table verifies scoped
            # bucket read permissions; a successful empty result is valid.
            flux = f'from(bucket: {json.dumps(source.bucket)}) |> range(start: -1m) |> limit(n: 1)'
            raw = api.request("POST", "/api/v2/query", raw=True, params={"org": source.organization},
                              headers={"Content-Type": "application/vnd.flux", "Accept": "text/csv"},
                              content=flux.encode())
            if raw.strip() and not (raw.lstrip().startswith("#") or raw.lstrip().startswith(",result,table,")):
                raise ApiError("InfluxDB вернул ответ неизвестного формата вместо CSV.")
            if any(row[:3] == ["", "error", "reference"] for row in csv.reader(raw.splitlines())):
                raise ApiError("Flux вернул ошибку при чтении bucket.")
            message = "Подключение доступно. Flux проверил доступ к организации и bucket."
        else:
            data = api.request("GET", "/api/v3/query_sql", params={
                "db": source.database, "q": "SHOW TABLES", "format": "json",
            })
            if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
                raise ApiError("InfluxDB 3 вернул ответ неизвестного формата вместо списка таблиц.")
            message = "Подключение доступно. InfluxDB 3 SQL проверил указанную базу."
        return DatasourceHealth(source.id, "ok", message, time.time())
    except ApiError as exc:
        return DatasourceHealth(source.id, "error", str(exc), time.time())


class ServerClient(PrometheusClient):
    """Proposed v1 contract; separate backend is not implemented here."""
    def publish(self, definition: MetricDefinition, source: Datasource) -> dict:
        return self.request("PUT", "/api/v1/metrics/" + quote(definition.name, safe=""),
                            json=definition.payload(source))

    def definitions(self) -> list[dict]:
        result = self.request("GET", "/api/v1/metrics")
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ApiError("Ожидался объект с массивом items.")
        return result["items"]

    def exposition(self) -> str:
        return self.request("GET", "/metrics", raw=True)
