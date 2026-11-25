import json
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest
from atropos_ui.clients import ApiError, HttpApi
from atropos_ui.detector_runtime import ReplayRuntime
from atropos_ui.live_runtime import LiveRuntime
from atropos_ui.runtime_client import RemoteLiveRuntime, RemoteReplayRuntime
from atropos_ui.runtime_server import RuntimeHTTPServer, RuntimeService
from atropos_ui.service_api import PostgresConfigRepository
from psycopg.conninfo import conninfo_to_dict
from test_integrity_fixes import BASE, SOURCE, MemoryAccount, account, data

TOKEN = 'test-runtime-service-token-' + 'a' * 40


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv('ATROPOS_RUNTIME_TOKEN', TOKEN)
    monkeypatch.delenv('ATROPOS_RUNTIME_TOKEN_FILE', raising=False)
    raw = data(40)
    alice, bob = account(raw, ['MAD']), MemoryAccount(username='bob')
    alice.workspace['metric_sets'] = [{'id': 'set', 'name': 'set', 'start': 'now-20s', 'end': 'now',
        'refresh_seconds': 5, 'members': [{'metric_id': 'm', 'detectors': {'MAD': {}}, 'peers': []}]}]
    stores = {'alice': alice, 'bob': bob}
    def store_factory(name):
        if name not in stores:
            raise ValueError('Account unavailable')
        return stores[name]
    from atropos_ui import detector_runtime
    original = detector_runtime.replay
    monkeypatch.setattr(detector_runtime, 'replay', lambda store, progress: original(
        store, progress, reader=lambda _: (raw, 0)))
    service = RuntimeService(replay=ReplayRuntime(),
        live=LiveRuntime(reader=lambda *a: (raw, 0), clock=lambda: BASE / 1e9 + 20, autostart=False),
        store_factory=store_factory, health_check=lambda: True)
    server = RuntimeHTTPServer(('127.0.0.1', 0), service, TOKEN)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f'http://127.0.0.1:{server.server_port}', service=service, alice=alice, bob=bob)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    service.shutdown()


def test_remote_replay_returns_same_metadata_and_isolates_accounts(backend):
    client = RemoteReplayRuntime(backend.url)
    assert client.start(backend.alice)
    backend.service.replay.state(backend.alice)['future'].result(timeout=10)
    result = client.result(backend.alice)
    assert result['metrics']['m']['detectors']['MAD']['metadata']['effective_parameters']['window'] == 20
    assert client.result(backend.bob) is None
    state = client.state(backend.alice)
    assert state['future'].done()
    assert state['result']['finished_at'] == result['finished_at']
    assert 'metrics' not in state['result']  # Polling does not download the whole result.


def test_remote_live_operations_keep_ownership_and_metadata(backend):
    client = RemoteLiveRuntime(backend.url)
    assert client.start(backend.alice, 'set')
    backend.service.live.tick()
    next(iter(backend.service.live.sessions.values()))['future'].result(timeout=10)
    session = client.summaries(backend.alice)[0]
    assert client.summaries(backend.bob) == []
    metadata = client.metadata(backend.alice, session['id'])
    assert metadata['execution_mode'] == 'incremental_live'
    options = client.component_options(backend.alice, session['id'])
    assert client.snapshot(backend.alice, session['id'], [options[0]['id']])['components']
    with pytest.raises(ApiError):
        client.snapshot(backend.bob, session['id'], [])
    with pytest.raises(ApiError):
        client.delete(backend.alice, session['id'])
    client.stop_set(backend.alice, 'set')
    client.delete(backend.alice, session['id'])
    assert client.summaries(backend.alice) == []


def test_health_metrics_and_service_authentication(backend):
    assert httpx.get(backend.url + '/healthz').json()['role'] == 'atropos-runtime'
    assert 'atropos_runtime_up 1' in httpx.get(backend.url + '/metrics').text
    response = httpx.post(backend.url + '/api/v1/runtime/replay/start',
                          json={'account': 'alice', 'arguments': {}})
    assert response.status_code == 401
    backend.service.health_check = lambda: (_ for _ in ()).throw(RuntimeError('private-dsn'))
    response = httpx.get(backend.url + '/healthz')
    assert response.status_code == 503 and 'private-dsn' not in response.text


@pytest.mark.parametrize('path,payload,status', [
    ('replay/__dict__', {'account': 'alice', 'arguments': {}}, 404),
    ('replay/start', {'account': 'alice', 'arguments': {'dsn': 'foreign'}}, 400),
    ('replay/start', {'account': 'disabled', 'arguments': {}}, 400),
    ('live/snapshot', {'account': 'alice', 'arguments': {'session_id': 'x', 'run': None, 'selected': ['a'] * 21}}, 400),
    ('live/metadata', {'account': 'alice', 'arguments': {'session_id': 'x', 'run': 'wrong'}}, 400),
    ('replay/start', {'account': 'alice', 'arguments': {}, 'injected': True}, 400),
])
def test_rpc_rejects_arbitrary_methods_and_invalid_arguments(backend, path, payload, status):
    response = httpx.post(backend.url + '/api/v1/runtime/' + path,
                          headers={'Authorization': 'Bearer ' + TOKEN}, json=payload)
    assert response.status_code == status


def test_runtime_request_size_and_malformed_json(backend):
    headers = {'Authorization': 'Bearer ' + TOKEN}
    for content, status in [(b'x' * 65537, 413), (b'not json', 400), (b'[]', 400)]:
        response = httpx.post(backend.url + '/api/v1/runtime/replay/start', headers=headers, content=content)
        assert response.status_code == status


def test_ui_selects_proxies_without_starting_local_workers(monkeypatch):
    from atropos_ui import live_ui, replay_ui
    monkeypatch.setenv('ATROPOS_RUNTIME_URL', 'http://server:8000')
    monkeypatch.setattr(live_ui, '_instance', None)
    monkeypatch.setattr(replay_ui, '_instance', None)
    def fail():
        pytest.fail('Container UI must not start local detector workers')
    monkeypatch.setattr(live_ui, 'LiveRuntime', fail)
    monkeypatch.setattr(replay_ui, 'ReplayRuntime', fail)
    monkeypatch.setattr(replay_ui, 'startup_accounts', fail)
    assert isinstance(live_ui.live_runtime(), RemoteLiveRuntime)
    assert isinstance(replay_ui.runtime(), RemoteReplayRuntime)


def test_remote_login_retries_without_crashing_when_backend_is_down(monkeypatch):
    from atropos_ui import replay_ui
    state, warnings = {'rebuild_after_login': True}, []
    monkeypatch.setattr(replay_ui.st, 'session_state', state)
    monkeypatch.setattr(replay_ui.st, 'warning', warnings.append)
    def fail(store):
        raise ApiError('connection failed')
    remote = SimpleNamespace(start=fail)
    monkeypatch.setattr(replay_ui, 'runtime', lambda: remote)
    store = SimpleNamespace(preferences=lambda: {'rebuild_on_login': True}, metrics=lambda: [1], metric_sets=lambda: [])
    assert replay_ui.on_login(store) is remote
    assert state['rebuild_after_login'] and warnings


def test_set_accounts_do_not_automatically_read_legacy_influx_history_on_login(monkeypatch):
    from atropos_ui import replay_ui
    monkeypatch.setattr(replay_ui.st, 'session_state', {'rebuild_after_login': True})
    remote = SimpleNamespace(start=lambda store: pytest.fail('Legacy import must remain explicit for set accounts'))
    monkeypatch.setattr(replay_ui, 'runtime', lambda: remote)
    store = SimpleNamespace(preferences=lambda: {'rebuild_on_login': True}, metrics=lambda: [1], metric_sets=lambda: [1])
    assert replay_ui.on_login(store) is remote


def test_runtime_token_file_and_unicode_accounts(monkeypatch, tmp_path):
    path = tmp_path / 'runtime-token'
    path.write_text(TOKEN, encoding='utf-8')
    monkeypatch.setenv('ATROPOS_RUNTIME_TOKEN_FILE', str(path))
    def handler(request):
        assert request.headers['Authorization'] == 'Bearer ' + TOKEN
        assert json.loads(request.content)['account'] == 'учётка'
        return httpx.Response(200, json={'data': True})
    assert RemoteReplayRuntime('http://server', transport=httpx.MockTransport(handler)).start(
        SimpleNamespace(username='учётка'))


def test_pg_environment_fallback_escapes_password_and_explicit_dsn_wins(monkeypatch):
    monkeypatch.delenv('ATROPOS_CONFIG_DATABASE_URL', raising=False)
    for key, value in {'PGHOST': 'config-postgres', 'PGPORT': '5432', 'PGUSER': 'account',
                       'PGPASSWORD': "test @ / ' \\", 'PGDATABASE': 'configuration'}.items():
        monkeypatch.setenv(key, value)
    parsed = conninfo_to_dict(PostgresConfigRepository()._dsn)
    assert parsed['host'] == 'config-postgres' and parsed['password'] == "test @ / ' \\"
    assert PostgresConfigRepository('postgresql://explicit/db')._dsn == 'postgresql://explicit/db'
    monkeypatch.setenv('ATROPOS_CONFIG_DATABASE_URL', 'postgresql://environment/db')
    assert PostgresConfigRepository()._dsn == 'postgresql://environment/db'


def test_container_loopback_mapping_keeps_stored_datasource_unchanged(monkeypatch):
    monkeypatch.setenv('ATROPOS_SOURCE_LOOPBACK_HOST', 'host.docker.internal')
    from dataclasses import replace
    source = replace(SOURCE, url='http://127.0.0.1:8086/prefix')
    def handler(request):
        assert str(request.url) == 'http://host.docker.internal:8086/prefix/health'
        return httpx.Response(200, json={'ok': True})
    assert HttpApi(source.url, datasource=source, transport=httpx.MockTransport(handler)).request('GET', '/health')
    assert source.url == 'http://127.0.0.1:8086/prefix'
    assert HttpApi('http://localhost:8000').url == 'http://localhost:8000'
