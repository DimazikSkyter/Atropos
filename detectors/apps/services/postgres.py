"""Shared PostgreSQL configuration repository. Every write is one transaction."""
import os
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .configuration import MetricDefinition, ServiceSettings, validate_references
from .datasources import Datasource, DatasourceService, validate_catalog

DEFAULT_DATABASE_URL = 'postgresql://atropos:atropos-local-config@127.0.0.1:55432/atropos_config'
LOCK_ID = 1096045135
SOURCE_FIELDS = tuple(field.name for field in fields(Datasource))
DEFINITION_FIELDS = tuple(field.name for field in fields(MetricDefinition))


class ConfigurationError(RuntimeError):
    """Safe operator-facing error: connection strings and SQL details stay private."""


def _columns(names):
    return sql.SQL(', ').join(map(sql.Identifier, names))


class PostgresConfigRepository:
    def __init__(self, dsn: str | None = None):
        configured = os.getenv('ATROPOS_CONFIG_DATABASE_URL')
        if dsn is None and configured is None and os.getenv('PGHOST'):
            configured = make_conninfo(host=os.environ['PGHOST'], port=os.getenv('PGPORT', '5432'),
                dbname=os.getenv('PGDATABASE', 'atropos_config'), user=os.getenv('PGUSER', 'atropos'),
                **({'password': os.environ['PGPASSWORD']} if 'PGPASSWORD' in os.environ else {}))
        self._dsn = dsn if dsn is not None else (configured if configured is not None else DEFAULT_DATABASE_URL)
        if not isinstance(self._dsn, str) or not self._dsn.strip():
            raise ConfigurationError('Задайте PostgreSQL DSN в ATROPOS_CONFIG_DATABASE_URL.')

    @contextmanager
    def _connection(self, *, autocommit=False):
        try:
            with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5,
                                application_name='atropos-config', autocommit=autocommit,
                                options='-c lock_timeout=5000 -c statement_timeout=15000') as db:
                yield db
        except psycopg.errors.UniqueViolation:
            raise ValueError('Имя и UID источника должны быть уникальными; default может быть только один.') from None
        except psycopg.errors.ForeignKeyViolation:
            raise ValueError('Источник используется сохранёнными метриками или был удалён.') from None
        except psycopg.Error:
            raise ConfigurationError(
                'Не удалось прочитать или сохранить конфигурацию PostgreSQL. '
                'Проверьте ATROPOS_CONFIG_DATABASE_URL и запустите инфраструктуру: make service-up.'
            ) from None

    @staticmethod
    def _lock(db):
        db.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))

    def migrate(self):
        """Apply the same versioned migrations used by the Compose migrator."""
        with self._connection(autocommit=True) as db:
            for path in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
                db.execute(path.read_text(encoding='utf-8'), prepare=False)

    @staticmethod
    def _sources(db):
        rows = db.execute(sql.SQL('SELECT {} FROM atropos_config.datasources ORDER BY id').format(
            _columns(SOURCE_FIELDS)))
        return [Datasource(**row) for row in rows]

    def sources(self):
        with self._connection() as db:
            return self._sources(db)

    @staticmethod
    def _definitions(db):
        return [MetricDefinition(**row) for row in db.execute(
            sql.SQL('SELECT {} FROM atropos_config.metric_definitions ORDER BY name').format(_columns(DEFINITION_FIELDS)))]

    def definitions(self):
        with self._connection() as db:
            return self._definitions(db)

    @staticmethod
    def _upsert(db, table, values, key):
        columns = tuple(values)
        assignments = sql.SQL(', ').join(
            sql.SQL('{} = EXCLUDED.{}').format(sql.Identifier(column), sql.Identifier(column))
            for column in columns if column != key
        )
        db.execute(sql.SQL(
            'INSERT INTO atropos_config.{} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}, updated_at = now()'
        ).format(sql.Identifier(table), _columns(columns), sql.SQL(', ').join(sql.Placeholder() for _ in columns),
                 sql.Identifier(key), assignments), tuple(values.values()))

    def _write_sources(self, db, current, updated):
        # Demote first, so switching the partial unique default index is safe.
        db.execute('UPDATE atropos_config.datasources SET is_default = false WHERE is_default')
        removed = [source.id for source in current if source.id not in {item.id for item in updated}]
        if removed:
            db.execute('DELETE FROM atropos_config.datasources WHERE id = ANY(%s)', (removed,))
        for source in updated:
            self._upsert(db, 'datasources', {**asdict(source), 'name_key': source.name.casefold()}, 'id')

    def edit_sources(self, change):
        with self._connection() as db:
            self._lock(db)
            current = tuple(self._sources(db))
            updated = validate_catalog(change(current))
            validate_references(updated, self._definitions(db))
            self._write_sources(db, current, updated)
            return updated

    def save(self, item):
        if isinstance(item, Datasource):
            return DatasourceService(self).save(item)
        if not isinstance(item, MetricDefinition):
            raise TypeError('Expected Datasource or MetricDefinition')
        with self._connection() as db:
            self._lock(db)
            validate_references(self._sources(db), [item])
            self._upsert(db, 'metric_definitions', self._definition_values(item), 'name')

    @staticmethod
    def _definition_values(item):
        values = asdict(item)
        values['drop_labels'] = Jsonb(list(item.drop_labels))
        values['extra_labels'] = Jsonb(item.extra_labels)
        return values

    def save_definition(self, item: MetricDefinition, *, previous_name=None):
        if not isinstance(item, MetricDefinition):
            raise TypeError('Expected MetricDefinition')
        with self._connection() as db:
            self._lock(db)
            existing = {definition.name for definition in self._definitions(db)}
            if previous_name is not None and previous_name not in existing:
                raise ValueError('Сохранённая метрика была удалена. Обновите каталог.')
            if item.name != previous_name and item.name in existing:
                raise ValueError('Метрика с таким именем уже есть в каталоге. Укажите другое имя.')
            validate_references(self._sources(db), [item])
            if previous_name is not None and previous_name != item.name:
                db.execute('DELETE FROM atropos_config.metric_definitions WHERE name = %s', (previous_name,))
            self._upsert(db, 'metric_definitions', self._definition_values(item), 'name')

    def delete(self, kind, key):
        if kind == 'source':
            return DatasourceService(self).delete(key)
        if kind != 'definition':
            raise ValueError('Неизвестный вид настройки.')
        with self._connection() as db:
            self._lock(db)
            db.execute('DELETE FROM atropos_config.metric_definitions WHERE name = %s', (key,))

    def settings(self):
        with self._connection() as db:
            row = db.execute('SELECT server_url, server_token_env FROM atropos_config.service_settings WHERE singleton').fetchone()
            if row is None:
                raise ConfigurationError('Настройки сервера отсутствуют. Примените миграции конфигурации.')
            return ServiceSettings(**row)

    def save_settings(self, settings):
        if not isinstance(settings, ServiceSettings):
            raise TypeError('Expected ServiceSettings')
        with self._connection() as db:
            self._lock(db)
            self._upsert(db, 'service_settings', {'singleton': True, **asdict(settings)}, 'singleton')

    def import_configuration(self, sources, definitions):
        """Merge a validated legacy catalog atomically; conflicts never overwrite data."""
        sources = validate_catalog(sources)
        definitions = tuple(definitions)
        validate_references(sources, definitions)
        with self._connection() as db:
            self._lock(db)
            current = tuple(self._sources(db))
            by_uid = {item.id: item for item in current}
            for source in sources:
                previous = by_uid.get(source.id)
                if previous is not None and previous != replace(source, is_default=previous.is_default, revision=previous.revision):
                    raise ValueError(f'Конфликт импорта источника {source.id}; существующие настройки сохранены.')
                by_uid[source.id] = previous if previous is not None else source
            # Existing default wins when importing into an already populated catalog.
            default = next((item.id for item in current if item.is_default),
                           next((item.id for item in sources if item.is_default), None))
            merged = validate_catalog(replace(item, is_default=item.id == default) for item in by_uid.values())
            existing = {item.name: item for item in self._definitions(db)}
            for item in definitions:
                if item.name in existing and existing[item.name] != item:
                    raise ValueError(f'Конфликт импорта определения {item.name}; существующие настройки сохранены.')
                existing[item.name] = item
            validate_references(merged, existing.values())
            self._write_sources(db, current, merged)
            for item in definitions:
                self._upsert(db, 'metric_definitions', self._definition_values(item), 'name')
        return len(sources), len(definitions)
