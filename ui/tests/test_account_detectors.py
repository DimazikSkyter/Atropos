import json
from uuid import uuid4

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from atropos_ui.auth import password_hash, verify_password
from atropos_ui.detector_runtime import (
    DETECTORS,
    correlation,
    defaults,
    replay,
    unary,
    validate_bindings,
)
from atropos_ui.influx import descriptors, parse_flux, read_source
from atropos_ui.service_api import (
    AccountStore,
    Datasource,
    MetricDefinition,
    account_hash,
    create_account,
)


def history(source='influx', *, count=70):
    lines = [',result,table,_time,_value,_field,_measurement,host']
    import pandas as pd
    for host, offset in [('a', 0), ('b', 1)]:
        for index in range(count):
            stamp = pd.Timestamp('2026-01-01', tz='UTC') + pd.Timedelta(seconds=15 * index)
            lines.append(f',,0,{stamp.isoformat()},{index + offset},value,cpu,{host}')
    return parse_flux('\n'.join(lines), source)[0]


def test_flux_preserves_all_tag_sets_and_nanoseconds_and_reports_nonnumeric():
    raw = '''#datatype,string,long,dateTime:RFC3339,double,string,string,string
,result,table,_time,_value,_field,_measurement,host
,,0,2026-01-01T00:00:00.123456789Z,1,value,cpu,a
,,0,2026-01-01T00:00:00.123456789Z,2,value,cpu,b
,,0,2026-01-01T00:00:15Z,NaN,value,cpu,a
'''
    data, skipped = parse_flux(raw, 'influx')
    metric = next(iter(data.values()))
    assert len(metric['series']) == 2 and skipped == 1
    assert metric['series'][0]['times'][0] % 1_000_000_000 == 123456789
    assert metric['series'][1]['values'] == [2.0]
    with pytest.raises(RuntimeError, match='повторный timestamp'):
        parse_flux(raw + ',,0,2026-01-01T00:00:00.123456789Z,9,value,cpu,a', 'influx')


def test_flux_queries_original_history_and_escapes_bucket(monkeypatch):
    monkeypatch.setenv('TEST_INFLUX_TOKEN', 'secret')
    source = Datasource('influx', 'Influx', 'http://influx', 'InfluxDB', auth_type='token',
                        token_env='TEST_INFLUX_TOKEN', organization='org', bucket='a"b')
    def request(req):
        assert req.headers['Authorization'] == 'Token secret'
        assert b'range(start: 0)' in req.content
        assert b'aggregateWindow' not in req.content
        assert b'"a\\"b"' in req.content
        return httpx.Response(200, text=',result,table,_time,_value,_field,_measurement\n')
    assert read_source(source, transport=httpx.MockTransport(request)) == ({}, 0)


def test_all_real_detectors_replay_fresh_state_and_warmup():
    series = next(iter(history(count=20).values()))['series'][0]
    for name in DETECTORS:
        validate_bindings({name: {}})
        assert defaults(name)
        if name != 'Correlation':
            assert unary(name, {}, series) == unary(name, {}, series)
    assert unary('SSA', {}, series)['status'] == 'warmup'
    with pytest.raises(ValueError):
        validate_bindings({'MAD': {'unknown_parameter': 1}})


def test_correlation_uses_exact_timestamps_and_never_pairs_a_series_with_itself():
    assert correlation({}, [{'times': [1], 'values': [1]}])['status'] == 'skipped'
    a = {'label': 'a', 'times': [1, 2, 3], 'values': [1, 2, 3]}
    b = {'label': 'b', 'times': [2, 3, 4], 'values': [2, 3, 4]}
    result = correlation({}, [a, b])
    assert result['series'][0]['times'] == [2, 3]
    assert result['series'][0]['scores'] == [None, None]


@pytest.fixture
def accounts(postgres_test_dsn):
    users = [f'test-{uuid4().hex}' for _ in range(2)]
    encoded = password_hash('account-password-123')
    for username in users:
        assert create_account(username, encoded, dsn=postgres_test_dsn)
    yield [AccountStore(user, postgres_test_dsn) for user in users]
    with accounts_cleanup(postgres_test_dsn) as db:
        db.execute('DELETE FROM atropos_config.accounts WHERE username = ANY(%s)', (users,))


def accounts_cleanup(dsn):
    from atropos_ui.storage import Store
    return Store(dsn)._connection()


def import_history(store, *, count=70):
    source = Datasource('influx', 'Influx', 'http://influx', 'InfluxDB', organization='org', bucket='metrics')
    store.save(source)
    raw = history(count=count)
    store.import_metrics(descriptors(raw, DETECTORS), expected_revision=store.snapshot()[1])
    return source, raw


def test_account_catalog_sources_bindings_and_results_are_isolated(accounts):
    alice, bob = accounts
    assert verify_password('account-password-123', account_hash(alice.username, alice._dsn))
    assert not create_account(alice.username, password_hash('different-password'), dsn=alice._dsn)
    source, raw = import_history(alice)
    assert bob.sources() == [] and bob.metrics() == [] and bob.load_results() is None
    metric = alice.metrics()[0]
    with pytest.raises(ValueError, match='не принадлежит'):
        bob.update_metric(metric['id'], {'name': 'stolen'}, expected_revision=bob.snapshot()[1])
    with pytest.raises(ValueError, match='используется'):
        alice.delete('source', source.id)
    # Even matching source IDs and output names are private to their respective owners.
    vm = Datasource('vm', 'VM', 'http://vm')
    for store in (alice, bob):
        store.save(vm)
        store.save_definition(MetricDefinition('same_name', 'vm', store.username))
    assert alice.definitions()[0].query == alice.username
    assert bob.definitions()[0].query == bob.username
    alice.save_preferences({**alice.preferences(), 'persist_results': True})
    result = replay(alice, reader=lambda source: (raw, 0))
    assert len(result['metrics'][metric['id']]['detectors']) == len(DETECTORS)
    assert alice.load_results() == result
    assert bob.load_results() is None
    alice.save_preferences({**alice.preferences(), 'persist_results': False})
    assert alice.load_results() is None
    assert not alice.save_results(result, alice.snapshot()[1])


def test_reimport_preserves_user_settings_and_stale_run_cannot_publish(accounts):
    store, _ = accounts
    _, raw = import_history(store)
    metric = store.metrics()[0]
    store.update_metric(metric['id'], {'name': 'My CPU', 'extra_labels': {'team': 'a'}, 'detectors': {'MAD': {'window': 4}}},
                        expected_revision=store.snapshot()[1])
    store.import_metrics(descriptors(raw, DETECTORS), expected_revision=store.snapshot()[1])
    assert store.metrics()[0]['name'] == 'My CPU'
    assert store.metrics()[0]['detectors'] == {'MAD': {'window': 4}}
    previous = store.snapshot()[1]
    store.save_preferences({**store.preferences(), 'persist_results': True})
    assert not store.save_results({'stale': True}, previous)
    assert store.load_results() is None
    with pytest.raises(ValueError, match='изменились'):
        store.update_metric(metric['id'], {'name': 'stale'}, expected_revision=previous)


def test_replay_reads_again_and_configuration_change_discards_old_result(accounts):
    store, _ = accounts
    _, raw = import_history(store)
    def reader(source):
        store.save_preferences(store.preferences())
        return raw, 0
    with pytest.raises(ValueError, match='во время прогона'):
        replay(store, reader=reader)
    metric = store.metrics()[0]
    store.update_metric(metric['id'], {'drop_labels': ['host']}, expected_revision=store.snapshot()[1])
    output = replay(store, reader=lambda source: (raw, 0))['metrics'][metric['id']]
    assert output['status'] == 'error' and 'одинаковыми' in output['message']


def test_results_page_renders_private_metric_and_clickable_legend(accounts, monkeypatch):
    store, other = accounts
    _, raw = import_history(store)
    # Different timestamps make incorrect detector/source reindexing observable.
    next(iter(raw.values()))['series'][1]['times'] = [
        t + 5_000_000_000 for t in next(iter(raw.values()))['series'][1]['times']]
    result = replay(store, reader=lambda source: (raw, 0))
    class Runtime:
        def state(self, store):
            return None
        def result(self, store):
            return result
        def start(self, store):
            return True
    monkeypatch.setattr('atropos_ui.pages.detectors.runtime', lambda: Runtime())
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.detectors import render
render(context())
''', default_timeout=20).run()
    assert not app.exception
    assert next(s for s in app.selectbox if s.label == 'Закреплённая метрика').value['id'] == store.metrics()[0]['id']
    assert len(app.get('plotly_chart')) == 1
    figure = json.loads(app.get('plotly_chart')[0].proto.spec)
    assert figure['layout']['legend']['itemclick'] == 'toggle'
    assert len(figure['data']) > 2
    assert other.metrics() == []
    revision = store.snapshot()[1]
    next(s for s in app.multiselect if s.label == 'Значения лейбла').set_value(['b']).run()
    assert not app.exception
    filtered = json.loads(app.get('plotly_chart')[0].proto.spec)['data']
    assert len(filtered) == 3  # Original + CUSUM + Trend, all for host=b.
    assert all('"host": "b"' in trace['name'] for trace in filtered)
    assert filtered[0]['y'][0] == 1.0
    assert all(trace['x'] == filtered[0]['x'] for trace in filtered)
    next(s for s in app.multiselect if s.label == 'Показать результаты детекторов').set_value(['Correlation']).run()
    assert not app.exception
    assert len(json.loads(app.get('plotly_chart')[0].proto.spec)['data']) == 1
    assert any('оба ряда' in item.value for item in app.info)
    next(s for s in app.multiselect if s.label == 'Значения лейбла').set_value(['a', 'b']).run()
    assert not app.exception
    assert len(json.loads(app.get('plotly_chart')[0].proto.spec)['data']) == 3
    next(s for s in app.multiselect if s.label == 'Значения лейбла').set_value([]).run()
    assert not app.exception
    assert len(app.get('plotly_chart')) == 0
    assert any('Нет рядов' in item.value for item in app.info)
    assert store.snapshot()[1] == revision


def test_real_account_login_selects_its_own_catalog(accounts, monkeypatch):
    from pathlib import Path
    store, other = accounts
    monkeypatch.setenv('ATROPOS_CONFIG_DATABASE_URL', store._dsn)
    monkeypatch.delenv('ATROPOS_UI_PASSWORD_HASH', raising=False)
    monkeypatch.delenv('ATROPOS_UI_DEMO', raising=False)
    monkeypatch.setattr('atropos_ui.replay_ui.runtime', lambda: None)
    monkeypatch.setattr('atropos_ui.replay_ui.on_login', lambda store: None)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=15).run()
    app.text_input[0].set_value(store.username)
    app.text_input[1].set_value('account-password-123')
    next(b for b in app.button if b.label == 'Войти').click().run()
    assert not app.exception
    assert app.session_state['identity'] == store.username
    # No source is inherited from any other account or the old shared catalog.
    assert store.sources() == [] and other.sources() == []
    next(b for b in app.button if b.label == 'Выйти').click().run()
    assert 'identity' not in app.session_state


def test_account_preferences_ui_and_startup_selection(accounts, monkeypatch):
    from atropos_ui.service_api import startup_accounts
    store, _ = accounts
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.account import render
render(context())
''').run()
    next(c for c in app.checkbox if c.label == 'Сохранять результаты детекторов').check()
    next(c for c in app.checkbox if c.label == 'Пересчитывать при запуске сервиса').uncheck()
    next(b for b in app.button if b.label == 'Сохранить настройки учётки').click().run()
    assert not app.exception
    assert store.preferences()['persist_results']
    assert store.username not in startup_accounts(store._dsn)


def test_runtime_restarts_when_configuration_changes_and_keeps_users_separate(monkeypatch):
    from atropos_ui.detector_runtime import ReplayRuntime
    class Store:
        _dsn = 'test'
        username = 'a'
        revision = 1
        def snapshot(self):
            return {}, self.revision
        def load_results(self):
            return None
    store = Store()
    calls = []
    def run(store, progress):
        calls.append(store.revision)
        if len(calls) == 1:
            store.revision = 2
            raise ValueError('changed')
        return {'revision': store.revision}
    monkeypatch.setattr('atropos_ui.detector_runtime.replay', run)
    instance = ReplayRuntime()
    try:
        instance.start(store)
        instance.state(store)['future'].result(timeout=10)
        assert calls == [1, 2]
        assert instance.result(store) == {'revision': 2}
        other = Store()
        other.username = 'b'
        assert instance.result(other) is None
    finally:
        instance.pool.shutdown(wait=True)
