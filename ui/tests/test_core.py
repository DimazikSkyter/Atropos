import json
import re

import httpx
import pytest

from atropos_ui.auth import password_hash, verify_password
from atropos_ui.clients import (
    METRIC_SEARCH_LIMIT,
    ApiError,
    PrometheusClient,
    ServerClient,
)
from atropos_ui.components import query_frame
from atropos_ui.models import Datasource, MetricDefinition, selector
from atropos_ui.service_api import MemoryConfigRepository


def test_auth():
    encoded = password_hash("very-long-test-password")
    assert verify_password("very-long-test-password", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("anything", "broken")
    assert not verify_password("anything", "pbkdf2_sha256$999999999$salt$hash")


def test_storage_protects_references():
    store = MemoryConfigRepository()
    source = Datasource("vm", "VM", "http://localhost:8428/")
    item = MetricDefinition("derived_value", "vm", "sum(up)")
    store.save(source)
    store.save(item)
    store.save(MetricDefinition("derived_value", "vm", "avg(up)"))
    assert store.definitions()[0].query == "avg(up)"
    assert store.sources()[0].url == "http://localhost:8428"
    with pytest.raises(ValueError, match="используется"):
        store.delete("source", "vm")
    store.delete("definition", "derived_value")
    store.delete("source", "vm")
    assert not store.sources()
    with pytest.raises(ValueError):
        store.save(item)


def test_selector_escapes_values():
    assert selector("up", {"instance": 'a"b\\c\nd'}) == '{__name__="up",instance="a\\"b\\\\c\\nd"}'


@pytest.mark.parametrize("url", ["file:///tmp/test", "http://u:p@host", "http://host?token=x", "http://host#fragment"])
def test_invalid_urls(url):
    with pytest.raises(ValueError):
        Datasource("x", "x", url)


def test_prometheus_endpoints_keep_tenant_prefix_and_encode_query(monkeypatch):
    monkeypatch.setenv("TEST_VM_TOKEN", "secret")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer secret"
        if request.url.path.endswith("values"):
            data = ["http_requests_total"]
        elif request.url.path.endswith("series"):
            data = [{"__name__": "up", "job": "test"}]
        else:
            data = {"resultType": "matrix", "result": []}
        return httpx.Response(200, json={"status": "success", "data": data})

    client = PrometheusClient("http://vm/select/42/prometheus", "TEST_VM_TOKEN", transport=httpx.MockTransport(handler))
    assert client.names('http') == ["http_requests_total"]
    assert requests[-1].url.path == '/select/42/prometheus/api/v1/label/__name__/values'
    assert requests[-1].url.params['match[]'] == '{__name__=~"(?i).*http.*"}'
    assert requests[-1].url.params['limit'] == str(METRIC_SEARCH_LIMIT + 1)
    assert client.series("up", 10, 20)[0]["job"] == "test"
    query = 'sum(rate(http_requests_total{job="a+b"}[5m]))'
    client.range(query, 10, 100, 15)
    assert requests[-1].url.path == "/select/42/prometheus/api/v1/query_range"
    assert requests[-1].url.params["query"] == query
    client.instant("up")
    assert requests[-1].url.path.endswith("/api/v1/query")


@pytest.mark.parametrize('search', ['', 'a', 'ab', '  ab  ', '   '])
def test_metric_search_rejects_short_input_without_contacting_source(search):
    def unexpected_request(request):
        pytest.fail('Short searches must not contact the source')

    client = PrometheusClient('http://vm', transport=httpx.MockTransport(unexpected_request))
    with pytest.raises(ValueError, match='минимум 3'):
        client.names(search)


@pytest.mark.parametrize('search', ['HTTP', 'foo.bar', 'a|b', 'a"b', 'a\\b', '.*x', 'x]y'])
def test_metric_search_escapes_literal_input_and_bounds_results(search):
    def handler(request):
        matcher = request.url.params['match[]']
        assert matcher.startswith('{__name__=~') and matcher.endswith('}')
        pattern = json.loads(matcher[len('{__name__=~'):-1])
        assert re.fullmatch(pattern, 'prefix_' + search.lower() + '_suffix')
        assert not re.fullmatch(pattern, 'unrelated_metric')
        assert request.url.params['limit'] == str(METRIC_SEARCH_LIMIT + 1)
        return httpx.Response(200, json={'status': 'success',
                                       'data': [f'metric_{i}' for i in range(1000)]})

    client = PrometheusClient('http://vm', transport=httpx.MockTransport(handler))
    assert len(client.names('  ' + search + '  ')) == METRIC_SEARCH_LIMIT + 1


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"status": "error", "error": "bad query"}),
    httpx.Response(200, text="<html>proxy error</html>"),
    httpx.Response(401, text="secret"),
    httpx.Response(302, headers={"Location": "https://other-host/"}),
])
def test_errors_are_not_empty_data_or_secret_leaks(response):
    client = PrometheusClient("http://vm", transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(ApiError) as error:
        client.instant("up")
    assert "secret" not in str(error.value)


def test_timeout_and_missing_token(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    with pytest.raises(ApiError, match="MISSING_TOKEN"):
        PrometheusClient("http://vm", "MISSING_TOKEN").instant("up")
    def timeout(request):
        raise httpx.ReadTimeout("sensitive details")
    with pytest.raises(ApiError, match="недоступен"):
        PrometheusClient("http://vm", transport=httpx.MockTransport(timeout)).instant("up")


@pytest.mark.parametrize("start,end,step", [(2, 1, 1), (1, 2, 0), (0, 12000, 1), (float("nan"), 1, 1)])
def test_invalid_window(start, end, step):
    with pytest.raises(ValueError):
        PrometheusClient("http://vm").range("up", start, end, step)


def test_server_contract_and_raw_exposition():
    requests = []
    exposition = '# HELP derived Description\n# TYPE derived gauge\nderived{job="a"} 42\n'
    def handler(request):
        requests.append(request)
        if request.url.path == "/metrics":
            return httpx.Response(200, text=exposition)
        if request.method == "PUT":
            return httpx.Response(200, json={"name": "derived", "status": "active"})
        return httpx.Response(200, json={"items": []})
    client = ServerClient("http://server", transport=httpx.MockTransport(handler))
    client.publish(MetricDefinition("derived", "vm", "sum(up)"), Datasource("vm", "VM", "http://vm", token_env="VM_TOKEN"))
    assert requests[-1].method == "PUT"
    assert requests[-1].url.path == "/api/v1/metrics/derived"
    payload = json.loads(requests[-1].content)
    assert payload["datasource"]["token_env"] == "VM_TOKEN"
    assert payload["query"] == "sum(up)"
    assert client.exposition() == exposition
    assert client.definitions() == []


def test_result_preserves_labels_and_nonfinite():
    data = {"resultType": "matrix", "result": [
        {"metric": {"instance": "a"}, "values": [[10, "1"], [20, "NaN"]]},
        {"metric": {"instance": "b"}, "values": [[10, "+Inf"], [20, "2"]]},
    ]}
    frame = query_frame(data)
    assert len(frame) == 4
    assert frame.series.nunique() == 2
    assert frame.value.isna().sum() == 2
    assert frame.raw_value.tolist() == ["1", "NaN", "+Inf", "2"]
    assert query_frame({"resultType": "scalar", "result": [10, "42"]}).value.iloc[0] == 42
    assert query_frame({"resultType": "string", "result": [10, "text"]}).raw_value.iloc[0] == "text"
    assert query_frame({"resultType": "vector", "result": []}).empty
    with pytest.raises(ApiError):
        query_frame({"resultType": "vector", "result": [{"metric": {}, "histogram": []}]})
