# UI, backend и PostgreSQL Atropos

`infra/docker-compose-service.yml` поднимает самостоятельный проект `atropos-service`:
PostgreSQL 17, одноразовые миграции, вычислительный сервер и UI.

```powershell
make service-up
make service-ps
```

UI — <http://localhost:8501>, backend health — <http://localhost:8000/healthz>.
Миграции завершаются до старта server, UI ждёт health backend.
`make service-down` останавливает контейнеры и сохраняет volume БД.
Локальная разработка: `make config-up`, затем `make ui-local`.

Сборка, создание учёток, секреты и параметры запуска описаны в
[инструкции контейнеров](../../docs/CONTAINERS.md).

## Что хранится

Схема `atropos_config`, SQL: `detectors/apps/services/migrations/001_configuration.sql` и `002_metric_catalog.sql`. Мигратор запускает все нумерованные SQL-файлы по порядку.

| Таблица | Содержание и ограничения |
|---|---|
| `datasources` | Все поля `Datasource`, UID PK, casefold-ключ уникального имени, ревизия, время создания/изменения; уникальный partial index на default |
| `metric_definitions` | Запрос, имя, описание, JSONB `drop_labels` и `extra_labels`; FK на источник с запретом удаления используемого источника |
| `service_settings` | Одна запись: URL вычислительного сервера и имя переменной с bearer token |
| `schema_migrations` | Версии применённых миграций |

Все изменения каталога и ссылок выполняются в транзакциях под общей advisory lock.
В непустом каталоге `DatasourceService` обеспечивает ровно один default. Источник
с зависимыми PromQL-определениями нельзя удалить или сменить на несовместимый тип.
Семантические изменения и переключение default увеличивают ревизию; сохранение
устаревшей формы возвращает конфликт. Начальный PostgreSQL-каталог пуст: добавьте
источники в UI либо импортируйте старую конфигурацию.

Значения секретов, пароль UI и DSN остаются в окружении процесса; в PostgreSQL
пишутся только ссылки `token_env`, `password_env`, `server_token_env`. Samples,
результаты запросов и состояние алгоритмов не являются конфигурацией.
Прежние `ATROPOS_UI_DATA_DIR`, `ATROPOS_SERVER_URL`, `ATROPOS_SERVER_TOKEN_ENV`
больше не определяют рабочую конфигурацию UI. URL и token reference сервера
редактируются на странице «Серверные метрики» либо командой:

```powershell
uv --directory ui run --locked python -m atropos_ui.config_db settings --server-url http://localhost:8000 --server-token-env ATROPOS_SERVER_TOKEN
```

Каталог метрик хранит описание запроса и изменения имени/тегов. Период и шаг
просмотра находятся только в Streamlit-сессии, samples в PostgreSQL не записываются.
Миграция 002 сохраняет прежние определения, добавляя пустые списки/словари тегов.

## Импорт прежних данных

```powershell
uv --directory ui run --locked python -m atropos_ui.config_db import-sqlite .data/ui.sqlite3
```

Команда читает старый файл в режиме read-only и загружает источники и определения
в PostgreSQL одной транзакцией. UID, ссылки и имена переменных с секретами
сохраняются; старые повторяющиеся имена получают суффиксы. При конфликте с уже
сохранёнными настройками либо сломанной ссылке импорт откатывается целиком.
Совпадающий импорт можно повторить. Если в PostgreSQL уже выбран default,
он сохраняется. Исходный SQLite-файл не изменяется и автоматически не импортируется.

## Backend и UI

UI уже использовал общий `DatasourceService` внутри процесса. Теперь оба
потребителя могут использовать один PostgreSQL-каталог:

```python
from apps.services import DatasourceService

service = DatasourceService.from_postgres()  # ATROPOS_CONFIG_DATABASE_URL
sources = service.list()
```

Репозиторий: `apps.services.postgres.PostgresConfigRepository`. Он не зависит от
Streamlit, закрывает соединения после каждой транзакции и не подменяет ошибку БД
пустым каталогом. HTTP runtime API детекторов и RAM-история теперь работают
в отдельном server-контейнере, см. [RUNTIME_API.md](../../ui/RUNTIME_API.md).
Офлайн-демо использует память отдельной Streamlit-сессии и не обращается к БД.

## Остановка и проверки

`make service-down` останавливает инфраструктуру с сохранением volume.
`make service-up` повторно запускает PostgreSQL и мигратор. `make ui` запускает весь контейнерный стек; `make ui-local` запускает хостовый UI.

```powershell
uv --directory ui run --locked pytest
uv --directory ui run --locked ruff check .
# PostgreSQL-интеграция: временная БД создаётся и удаляется автоматически.
$env:ATROPOS_TEST_DATABASE_URL = $env:ATROPOS_CONFIG_DATABASE_URL
uv --directory ui run --locked pytest tests/test_postgres.py
```

Для интеграционных тестов нужен PostgreSQL-пользователь с правом CREATE DATABASE;
тесты создают отдельную БД с именем `atropos_config_test_<uuid>` и не очищают
рабочую БД. Если переменная тестового DSN не задана, эти тесты пропускаются.

Транзакции и блокировки соответствуют документации
[Psycopg](https://www.psycopg.org/psycopg3/docs/basic/transactions.html) и
[PostgreSQL 17](https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS).
