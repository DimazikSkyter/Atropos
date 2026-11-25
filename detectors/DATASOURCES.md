# Сервис источников данных

`apps.services.DatasourceService` управляет конфигурацией источников
VictoriaMetrics, Prometheus, InfluxDB и ClickHouse. UI использует этот же сервис
на существующей вкладке «Источники данных».

Подход соответствует основным действиям Grafana: постоянный UID, имя подключения,
тип, URL, авторизация, настройки конкретного хранилища, источник по умолчанию и
«Сохранить и проверить». Сервис не зависит от Streamlit или HTTP-клиента.

```python
from apps.services import DatasourceService

# Общий PostgreSQL-каталог UI и backend; DSN из ATROPOS_CONFIG_DATABASE_URL.
sources = DatasourceService.from_postgres()
vm = sources.create('VictoriaMetrics prod', 'http://localhost:8428')
ch = sources.create(
    'ClickHouse metrics', 'http://localhost:8123', 'ClickHouse',
    database='observability', auth_type='basic',
    username='reader', password_env='CLICKHOUSE_PASSWORD',
)
sources.set_default(ch.uid)
sources.update(vm.uid, name='VictoriaMetrics main', timeout_seconds=10)
assert sources.get(vm.uid).name == 'VictoriaMetrics main'
assert sources.default().uid == ch.uid
```

| Метод | Действие |
|---|---|
| `create(name, url, kind, **settings)` | Создать подключение с новым UID |
| `save(Datasource(...))` | Сохранить конфигурацию по UID |
| `update(uid, **changes)` | Поменять настройки с сохранением UID |
| `list(kind=None, search='')` / `get(uid)` | Найти подключения |
| `default()` / `set_default(uid)` | Получить / сменить источник по умолчанию |
| `delete(uid)` | Удалить конфигурацию подключения |
| `test_connection(uid)` | Вызвать внедрённый адаптер проверки |
| `from_postgres(dsn=None, checker=None)` | Создать сервис над общим PostgreSQL-каталогом |

Имена уникальны без учёта регистра. В непустом каталоге ровно один default:
первый добавленный источник выбирается автоматически, изменение default снимает
флаг с остальных. После удаления default выбирается оставшийся источник по имени.
Настройки и переключение default применяются атомарно в репозитории.

`Datasource.id` и `Datasource.uid` обозначают один постоянный идентификатор.
Первые поля модели совместимы с прежним UI: `id`, `name`, `url`, `kind`, `token_env`.
Дополнительно доступны `is_default`, `auth_type`, `username`, `password_env`,
`database`, `organization`, `bucket`, `influx_version`, `timeout_seconds`,
`tls_verify`, `min_interval_seconds`, `revision`.

| Тип | Настройки и поддержанная проверка |
|---|---|
| VictoriaMetrics | HTTP(S) URL, включая cluster tenant prefix; `vector(1)` через Prometheus API |
| Prometheus | HTTP(S) URL; `vector(1)` через Prometheus API |
| InfluxDB 1.x | Database, при необходимости Basic auth; `SHOW MEASUREMENTS LIMIT 1` через InfluxQL |
| InfluxDB 2.x | Organization, bucket, API token; короткий read-only Flux-запрос к bucket |
| InfluxDB 3.x Core | Database, API token; `SHOW TABLES` через HTTP SQL API |
| ClickHouse | HTTP-интерфейс, database, при необходимости Basic auth; `SELECT 1` с `readonly=2` |

Авторизация: `none`, `basic`, `bearer` для VM/Prometheus; `none`/`basic` для
ClickHouse и InfluxDB 1; `none`/`token` для InfluxDB 2/3. Пароль и токен задаются
ссылками на переменные окружения: значения секретов в PostgreSQL не записываются.
Старое поле `token_env` автоматически включает Bearer для VM/Prometheus.
Timeout — 1–300 секунд, минимальный шаг — 1–86400 секунд, TLS проверяется по умолчанию.

HTTP-адаптеры находятся в `ui/src/atropos_ui/clients.py`; они сохраняют proxy/tenant
prefix, не следуют redirects и не показывают тела ошибок с потенциальными секретами.
Проверка возвращает `DatasourceHealth(uid, status, message, checked_at)`.
`status='ok'` означает успешный проверочный запрос, `error` — ошибку, `demo` —
офлайн-демонстрацию без проверки реального соединения.

«Сохранить и проверить» сначала сохраняет конфигурацию, затем проверяет доступ:
ошибка проверки оставляет настройки доступными для исправления. В UI результаты
проверок привязаны к сохранённым настройкам; изменение подключения сбрасывает статус.

UI хранит подключения в общем PostgreSQL-репозитории сервиса. Старый каталог
импортируется явно через `python -m atropos_ui.config_db import-sqlite PATH`: UID, URL, token references и ссылки из определений
сохраняются. Старые повторяющиеся имена получают суффикс `(2)`, `(3)` и т.д.
Удаление источника, используемого определением метрики, блокируется. Такой источник
также нельзя сменить на тип, несовместимый с сохранённым PromQL.

Редактор PromQL продолжает использовать VM/Prometheus. InfluxDB/ClickHouse можно
добавлять, редактировать, выбирать default и проверять на вкладке источников;
универсальный редактор SQL/Flux и вычисление метрик из этих источников в эту задачу
не входят. Научные модели значений и `MetricService` при этом не менялись.

Постоянный репозиторий расположен в `apps.services.postgres`, а не в UI.
Там же хранятся общие определения `MetricDefinition` и настройки `ServiceSettings`.
`DatasourceService()` остаётся вариантом в памяти для изолированных вычислений
и тестов; рабочий UI всегда внедряет PostgreSQL. Изменение источника увеличивает
ревизию, устаревшее сохранение отклоняется без потери данных.
Схема и инфраструктура: [infra/service/README.md](../infra/service/README.md).

## Проверки

```powershell
# Из detectors
uv run python -m unittest tests.test_datasources -v
# Из ui
uv run pytest
uv run ruff check .
```

Тесты покрывают модель, UID/default, транзакции PostgreSQL и импорт SQLite, защиту ссылок,
HTTP-протоколы, секреты, ошибки и сценарии создания/редактирования/удаления в UI.
Сетевые ответы внешних источников подменяются. Интеграционные тесты конфигурации используют настоящий PostgreSQL и отдельную временную БД.

Основа поведения: [Grafana data source management](https://grafana.com/docs/grafana/latest/administration/data-source-management/),
[Grafana InfluxDB settings](https://grafana.com/docs/grafana/latest/datasources/influxdb/configure/).
Протоколы проверок: [InfluxDB 2 query API](https://docs.influxdata.com/influxdb/v2/api/query/),
[InfluxDB 3 Core HTTP API](https://docs.influxdata.com/influxdb3/core/query-data/execute-queries/influxdb-v3-api/),
[ClickHouse HTTP interface](https://clickhouse.com/docs/concepts/features/interfaces/http).
