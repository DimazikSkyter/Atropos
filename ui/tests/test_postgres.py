import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from atropos_ui.models import Datasource, MetricDefinition
from atropos_ui.service_api import (
    DatasourceService,
    ServiceSettings,
    read_legacy_configuration,
)
from atropos_ui.storage import ConfigurationError, Store


def test_persistence_shared_service_roundtrip_all_fields(pg_store, postgres_test_dsn):
    service = DatasourceService.from_postgres(postgres_test_dsn)
    for kind, settings in [
        ('VictoriaMetrics', dict(auth_type='bearer', token_env='VM_TOKEN')),
        ('Prometheus', dict(auth_type='basic', username='reader', password_env='PROM_PASSWORD')),
        ('ClickHouse', dict(database='metrics')),
        ('InfluxDB', dict(influx_version='1', database='metrics')),
        ('InfluxDB', dict(organization='org', bucket='bucket', token_env='INFLUX_TOKEN')),
        ('InfluxDB', dict(influx_version='3', database='metrics', token_env='INFLUX3_TOKEN')),
    ]:
        source = service.create(f'{kind} {len(service.list())}', 'https://proxy/prefix', kind,
                                timeout_seconds=7.5, tls_verify=False, min_interval_seconds=60, **settings)
        assert DatasourceService(Store(postgres_test_dsn)).get(source.id) == source
    first = service.list()[0]
    pg_store.save(MetricDefinition('derived', first.id, 'sum(up)', 'description', 45))
    assert Store(postgres_test_dsn).definitions() == pg_store.definitions()
    pg_store.save_settings(ServiceSettings('http://compute:8000/prefix', 'SERVER_TOKEN'))
    pg_store.migrate()
    assert Store(postgres_test_dsn).settings() == ServiceSettings('http://compute:8000/prefix', 'SERVER_TOKEN')
    assert len(service.list()) == 6


def test_default_references_revision_and_rollback(pg_store):
    service = DatasourceService(pg_store)
    vm = service.create('VM', 'http://vm')
    prom = service.create('Prom', 'http://prom', 'Prometheus')
    pg_store.save(MetricDefinition('derived', vm.id, 'up'))
    before = service.list()
    with pytest.raises(ValueError, match='используется'):
        service.delete(vm.id)
    with pytest.raises(ValueError, match='PromQL'):
        service.update(vm.id, kind='ClickHouse', is_default=True)
    with pytest.raises(ValueError, match='уникальн'):
        service.update(prom.id, name='vm', is_default=True)
    assert service.list() == before
    changed = service.update(vm.id, url='http://new-vm')
    assert changed.revision == vm.revision + 1
    with pytest.raises(ValueError, match='ревизии'):
        service.save(replace(vm, name='Stale edit'))
    service.set_default(prom.id)
    assert service.default().id == prom.id
    service.delete(prom.id)
    assert service.default().id == vm.id
    pg_store.delete('definition', 'derived')
    service.delete(vm.id)
    assert service.list() == ()


def test_concurrent_writes_do_not_lose_sources_or_default(pg_store, postgres_test_dsn):
    def create(index):
        return DatasourceService.from_postgres(postgres_test_dsn).create(
            f'VM {index}', f'http://vm-{index}', is_default=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        created = list(pool.map(create, range(8)))
    restored = pg_store.sources()
    assert {item.id for item in restored} == {item.id for item in created}
    assert sum(item.is_default for item in restored) == 1


def test_database_foreign_key_blocks_direct_orphan_delete(pg_store):
    source = pg_store.save(Datasource('vm', 'VM', 'http://vm'))
    pg_store.save(MetricDefinition('derived', source.id, 'up'))
    with pytest.raises(ValueError, match='используется'):
        with pg_store._connection() as db:
            db.execute('DELETE FROM atropos_config.datasources WHERE id = %s', (source.id,))
    assert pg_store.sources() == [source]


def test_connections_close_on_success_and_rollback(pg_store, monkeypatch):
    opened = []
    original = psycopg.connect
    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        opened.append(db)
        return db
    monkeypatch.setattr(psycopg, 'connect', connect)
    service = DatasourceService(pg_store)
    service.create('VM', 'http://vm')
    service.list()
    with pytest.raises(ValueError):
        service.create('vm', 'http://other')
    assert opened and all(db.closed for db in opened)


def legacy_file(path, *, bad_reference=False):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE config(kind TEXT, id TEXT, body TEXT, PRIMARY KEY(kind,id))')
        for uid in ('a', 'b'):
            body = dict(id=uid, name='Legacy', url='http://vm', kind='VictoriaMetrics', token_env='VM_TOKEN')
            db.execute('INSERT INTO config VALUES(?,?,?)', ('source', uid, json.dumps(body)))
        item = MetricDefinition('derived', 'missing' if bad_reference else 'a', 'up')
        db.execute('INSERT INTO config VALUES(?,?,?)', ('definition', item.name, json.dumps(asdict(item))))


def test_legacy_import_read_only_uid_references_and_idempotence(pg_store, tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    legacy_file(path)
    before = path.read_bytes()
    config = read_legacy_configuration(path)
    assert pg_store.import_configuration(*config) == (2, 1)
    assert pg_store.import_configuration(*config) == (2, 1)
    assert path.read_bytes() == before
    assert {item.id for item in pg_store.sources()} == {'a', 'b'}
    assert {item.name for item in pg_store.sources()} == {'Legacy', 'Legacy (2)'}
    assert all(item.token_env == 'VM_TOKEN' for item in pg_store.sources())
    assert pg_store.definitions()[0].datasource_id == 'a'
    path.unlink()  # The read-only SQLite connection was closed on Windows too.


def test_legacy_merge_preserves_existing_default_and_is_repeatable(pg_store, tmp_path):
    pg_store.save(Datasource('existing', 'Existing', 'http://existing'))
    path = tmp_path / 'legacy.sqlite3'
    legacy_file(path)
    config = read_legacy_configuration(path)
    pg_store.import_configuration(*config)
    pg_store.import_configuration(*config)
    assert DatasourceService(pg_store).default().id == 'existing'


def test_bad_legacy_reference_and_conflict_do_not_partially_import(pg_store, tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    legacy_file(path, bad_reference=True)
    with pytest.raises(ValueError):
        read_legacy_configuration(path)
    assert not pg_store.sources()
    pg_store.save(Datasource('b', 'Conflicting', 'http://existing'))
    before = pg_store.sources()
    with pytest.raises(ValueError, match='Конфликт'):
        pg_store.import_configuration([Datasource('a', 'A', 'http://a', is_default=True),
                                       Datasource('b', 'B', 'http://b')], [])
    assert pg_store.sources() == before


def test_ui_writes_are_visible_to_backend_and_next_ui_session(pg_store, postgres_test_dsn, monkeypatch):
    monkeypatch.delenv('ATROPOS_UI_DEMO', raising=False)
    monkeypatch.setenv('ATROPOS_CONFIG_DATABASE_URL', postgres_test_dsn)
    page = '''
from atropos_ui.context import context
from atropos_ui.pages.datasources import render
render(context())
'''
    app = AppTest.from_string(page, default_timeout=15).run()
    assert not app.exception
    next(item for item in app.text_input if item.label == 'Название').set_value('UI PostgreSQL source')
    next(item for item in app.button if item.label == 'Сохранить').click().run()
    assert not app.exception
    backend = DatasourceService.from_postgres(postgres_test_dsn)
    source = backend.list()[0]
    assert source.name == 'UI PostgreSQL source'
    backend.update(source.id, name='Changed by backend')
    next_session = AppTest.from_string(page, default_timeout=15).run()
    assert not next_session.exception
    assert next_session.dataframe[0].value.iloc[0]['Название'] == 'Changed by backend'


def test_stale_ui_form_does_not_overwrite_backend_edit(pg_store, postgres_test_dsn, monkeypatch):
    monkeypatch.delenv('ATROPOS_UI_DEMO', raising=False)
    monkeypatch.setenv('ATROPOS_CONFIG_DATABASE_URL', postgres_test_dsn)
    source = pg_store.save(Datasource('vm', 'VM', 'http://vm'))
    page = '''
from atropos_ui.context import context
from atropos_ui.pages.datasources import render
render(context())
'''
    app = AppTest.from_string(page, default_timeout=15).run()
    next(item for item in app.selectbox if item.label == 'Подключение').select_index(1).run()
    DatasourceService(pg_store).update(source.id, name='Backend revision')
    next(item for item in app.text_input if item.label == 'Название').set_value('Stale UI edit')
    next(item for item in app.button if item.label == 'Сохранить').click().run()
    assert not app.exception
    assert any('ревизии' in error.value for error in app.error)
    assert DatasourceService(pg_store).get(source.id).name == 'Backend revision'


def test_ui_server_settings_persist_and_are_used_by_client(pg_store, postgres_test_dsn, monkeypatch):
    monkeypatch.delenv('ATROPOS_UI_DEMO', raising=False)
    monkeypatch.setenv('ATROPOS_CONFIG_DATABASE_URL', postgres_test_dsn)
    page = '''
from atropos_ui.context import context
from atropos_ui.pages.server import render
render(context())
'''
    app = AppTest.from_string(page, default_timeout=15).run()
    next(item for item in app.text_input if item.label == 'URL сервера').set_value('http://new-server:8000')
    next(item for item in app.text_input if item.label == 'Переменная окружения с токеном сервера').set_value('NEW_SERVER_TOKEN')
    next(item for item in app.button if item.label == 'Сохранить настройки сервера').click().run()
    assert not app.exception
    assert pg_store.settings() == ServiceSettings('http://new-server:8000', 'NEW_SERVER_TOKEN')
    from atropos_ui.context import context
    assert context().server_client().url == 'http://new-server:8000'
    assert Store(postgres_test_dsn).settings().server_token_env == 'NEW_SERVER_TOKEN'


def test_unavailable_database_does_not_leak_credentials():
    store = Store('postgresql://reader:sensitive-password@127.0.0.1:1/unavailable')
    with pytest.raises(ConfigurationError) as error:
        store.sources()
    assert 'sensitive-password' not in str(error.value)
    assert 'ATROPOS_CONFIG_DATABASE_URL' in str(error.value)


def test_cli_starts_in_fresh_process_and_uses_postgres(pg_store, postgres_test_dsn):
    env = {**os.environ, 'ATROPOS_CONFIG_DATABASE_URL': postgres_test_dsn, 'PYTHONUTF8': '1'}
    result = subprocess.run(
        [sys.executable, '-B', '-c', 'from atropos_ui.storage import Store; assert Store().sources() == []'],
        env=env, capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stderr
    for arguments in [('migrate',), ('settings', '--server-url', 'http://cli-server:8000')]:
        result = subprocess.run(
            [sys.executable, '-B', '-m', 'atropos_ui.config_db', *arguments],
            env=env, capture_output=True, text=True, encoding='utf-8', timeout=30,
        )
        assert result.returncode == 0, result.stderr
    assert pg_store.settings().server_url == 'http://cli-server:8000'


def test_catalog_transform_roundtrip_rename_and_duplicate_rollback(pg_store, postgres_test_dsn):
    pg_store.save(Datasource('vm', 'VM', 'http://vm'))
    item = MetricDefinition('cpu_saved', 'vm', 'max by (name) (rate(cpu[5m]))',
                            drop_labels=('job', 'instance'), extra_labels={'team': 'ops', 'area': 'Москва'})
    pg_store.save_definition(item)
    assert Store(postgres_test_dsn).definitions() == [item]
    with pytest.raises(ValueError, match='уже есть'):
        pg_store.save_definition(item)
    renamed = replace(item, name='cpu_renamed', extra_labels={'team': 'new'})
    pg_store.save_definition(renamed, previous_name=item.name)
    assert Store(postgres_test_dsn).definitions() == [renamed]
    pg_store.save_definition(item)
    before = pg_store.definitions()
    with pytest.raises(ValueError, match='уже есть'):
        pg_store.save_definition(renamed, previous_name=item.name)
    assert pg_store.definitions() == before


def test_catalog_migration_preserves_existing_definitions(pg_store):
    pg_store.save(Datasource('vm', 'VM', 'http://vm'))
    try:
        with pg_store._connection() as db:
            db.execute('ALTER TABLE atropos_config.metric_definitions DROP COLUMN drop_labels, DROP COLUMN extra_labels')
            db.execute("INSERT INTO atropos_config.metric_definitions(name, datasource_id, query) VALUES ('legacy', 'vm', 'up')")
        pg_store.migrate()
        assert pg_store.definitions() == [MetricDefinition('legacy', 'vm', 'up')]
        pg_store.migrate()
        assert pg_store.definitions()[0].extra_labels == {}
    finally:
        pg_store.migrate()


def test_query_ui_persists_name_and_tag_operations_in_postgres(pg_store, postgres_test_dsn, monkeypatch):
    from atropos_ui.demo import DemoClient
    pg_store.save(Datasource('vm', 'VM', 'http://vm'))
    monkeypatch.delenv('ATROPOS_UI_DEMO', raising=False)
    monkeypatch.setenv('ATROPOS_CONFIG_DATABASE_URL', postgres_test_dsn)
    client = DemoClient()
    original = client.instant
    def instant(query):
        data = original(query)
        data['result'][0]['metric']['job'] = 'exporter'
        return data
    client.instant = instant
    monkeypatch.setattr('atropos_ui.context.Context.source_client', lambda self, source: client)
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.queries import render
render(context())
''', default_timeout=15).run()
    next(item for item in app.button if item.label == 'Текущее значение').click().run()
    next(item for item in app.text_input if item.label == 'Имя метрики').set_value('ui_cpu')
    next(item for item in app.multiselect if item.label == 'Удалить теги').set_value(['job'])
    next(item for item in app.text_input if item.label == 'Новый тег').set_value('team')
    next(item for item in app.text_input if item.label == 'Значение тега').set_value('ops')
    next(item for item in app.button if item.label == 'Добавить тег').click().run()
    next(item for item in app.button if item.label == 'Добавить в каталог').click().run()
    assert not app.exception
    saved = Store(postgres_test_dsn).definitions()[0]
    assert saved.name == 'ui_cpu' and saved.query == 'up'
    assert saved.drop_labels == ('job',) and saved.extra_labels == {'team': 'ops'}
    transformed = saved.transform_result(instant('up'))
    assert transformed['result'][0]['metric'] == {'__name__': 'ui_cpu', 'instance': 'node-a', 'team': 'ops'}
