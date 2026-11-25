import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from atropos_ui.models import Datasource
from atropos_ui.service_api import MemoryConfigRepository, ServiceSettings
from atropos_ui.storage import Store


@pytest.fixture
def ui_store(monkeypatch):
    store = MemoryConfigRepository()
    store.save(Datasource('local-vm', 'Local VictoriaMetrics', 'http://localhost:8428'))
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    monkeypatch.setattr('atropos_ui.auth.create_account', lambda *a, **kw: True)
    monkeypatch.setattr('atropos_ui.auth.account_hash', lambda name: os.getenv('ATROPOS_UI_PASSWORD_HASH')
                        if name == os.getenv('ATROPOS_UI_USERNAME') else None)
    monkeypatch.setattr('atropos_ui.replay_ui.runtime', lambda: None)
    return store


@pytest.fixture(scope='session')
def postgres_test_dsn():
    admin_dsn = os.getenv('ATROPOS_TEST_DATABASE_URL')
    if not admin_dsn:
        pytest.skip('Set ATROPOS_TEST_DATABASE_URL to run PostgreSQL integration tests')
    database = 'atropos_config_test_' + uuid4().hex
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    dsn = make_conninfo(admin_dsn, dbname=database)
    try:
        Store(dsn).migrate()
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database)))


@pytest.fixture
def pg_store(postgres_test_dsn, monkeypatch):
    store = Store(postgres_test_dsn)
    with store._connection() as db:
        db.execute('TRUNCATE atropos_config.metric_definitions, atropos_config.datasources')
    store.save_settings(ServiceSettings())
    # These legacy repository/UI tests intentionally use their explicitly injected store.
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    return store
