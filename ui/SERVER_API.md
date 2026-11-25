# Предлагаемый контракт Atropos Metrics Server v1

Это спецификация будущего сервера производных PromQL-метрик. Уже реализованный
вычислительный backend детекторов описан отдельно в [RUNTIME_API.md](RUNTIME_API.md).
В Docker UI обращается к этому backend; приведённые ниже endpoints PromQL пока не реализованы. Изменения контракта локализованы в `ServerClient`.

## Создание/обновление определения

`PUT /api/v1/metrics/{name}`, JSON, идемпотентное обновление по имени:

```json
{
  "name": "atropos_request_rate",
  "datasource_id": "local-vm",
  "query": "sum(rate(http_requests_total[5m]))",
  "description": "Request rate",
  "interval_seconds": 30,
  "datasource": {
    "id": "local-vm",
    "name": "Local VictoriaMetrics",
    "url": "http://localhost:8428",
    "kind": "VictoriaMetrics",
    "token_env": "VM_TOKEN",
    "auth_type": "bearer",
    "timeout_seconds": 20,
    "tls_verify": true,
    "min_interval_seconds": 15
  }
}
```

Ответ 200/201: `{"name":"atropos_request_rate","status":"active"}`. Ошибки: 400/422 невалидное определение/PromQL, 401/403 доступ, 409 конфликт. URL источника должен быть доступен **из среды сервера**; localhost относится к ней. `token_env` — ссылка на секрет в окружении сервера, не сам токен. При использовании контейнеров задайте общий доступный hostname источника для UI и сервера.

UI передаёт сериализованную общую модель `Datasource`: также возможны `is_default`,
`username`, `password_env`, `database`, `organization`, `bucket`, `influx_version`.
`password_env` — ссылка на секрет для Basic auth. Сервер должен применять
`auth_type`, timeout и TLS при создании клиента. Контракт определений этой версии
ограничен VM/Prometheus; UI не отправляет SQL/Flux в endpoint PromQL-метрик.

Сервер проверяет имя, доступ к источнику и права оператора, периодически выполняет instant query, переименовывает выходной `__name__`, сохраняет остальные labels и историю. Для первой версии принимаются scalar и float instant-vector; string, range-vector и native histograms сервер отклоняет как неподдерживаемые. Scalar превращается в gauge без дополнительных labels. Пустой результат не заменяется нулём. Ошибка вычисления не выдаётся за свежее значение.

`GET /api/v1/metrics` → `{"items":[{"name":"atropos_request_rate","datasource_id":"local-vm","query":"sum(rate(http_requests_total[5m]))","interval_seconds":30,"status":"active"}]}`.

## Текущее значение для VM / Prometheus

`GET /metrics` возвращает **Prometheus text exposition format**, `Content-Type: text/plain; version=0.0.4; charset=utf-8`:

```text
# HELP atropos_request_rate Request rate
# TYPE atropos_request_rate gauge
atropos_request_rate{job="api"} 12.5
```

Endpoint предназначен для scrape VM/Prometheus; возвращает только последние актуальные вычисленные samples. Сервер должен определить stale TTL (предлагается `2 * interval_seconds`) и исключать устаревшие/ошибочные samples, а состояние вычислений возвращать в списке определений. Повторный scrape не запускает вычисление. Нужны корректное экранирование HELP/labels и отсутствие конфликтующих label sets.

Пример задания для VictoriaMetrics (добавляется в её scrape config отдельно, когда сервер появится):

```yaml
scrape_configs:
  - job_name: atropos-derived
    scrape_interval: 30s
    static_configs:
      - targets: ["host.docker.internal:8000"]
```

## Текущее значение и серия для локального графика

Два read-only endpoint с форматом ответа Prometheus HTTP API:

- `GET /api/v1/query?query=atropos_request_rate` → `status=success`, `data.resultType=vector`, `data.result=[{"metric":{"__name__":"atropos_request_rate","job":"api"},"value":[1790416800,"12.5"]}]`.
- `GET /api/v1/query_range?query=atropos_request_rate&start=1790413200&end=1790416800&step=30` → `status=success`, `data.resultType=matrix`, `data.result=[{"metric":{"__name__":"atropos_request_rate","job":"api"},"values":[[1790413200,"11.2"],[1790413230,"12.5"]]}]`.

`start/end` — Unix seconds UTC, `step` — секунды. Внешняя оболочка: `{"status":"success","data":{...}}`. Нет данных — пустой `result`. Ошибка — HTTP 4xx/5xx и `{"status":"error","errorType":"...","error":"..."}`. API может быть реализован проксированием запросов к отдельному хранилищу истории производных метрик. Если backend поддерживает только селекторы, он должен явно отклонять прочие выражения; UI не вычисляет PromQL локально.

Оба endpoint читают историю **выходных** метрик; UI не заменяет её повторным вычислением исходного запроса в VM. `/metrics` — текущий scrape snapshot, `/query_range` — сохранённая история. Историю и retention реализует будущий сервер.

UI передаёт bearer token, если задан `ATROPOS_SERVER_TOKEN_ENV`. Прямой переход по ссылке `/metrics` не переносит token из процесса UI в браузер; кнопка получения текста использует серверный клиент с его авторизацией.
