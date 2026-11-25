import copy
import json
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from atropos_ui.detector_workspace import attach_detectors
from atropos_ui.metric_export import (
    metric_csv,
    metric_json,
    result_entities,
    write_metric_influx,
)
from atropos_ui.metric_reader import read_metric_window
from atropos_ui.service_api import (
    AccountStore,
    Datasource,
    MetricDefinition,
    catalog_metrics,
    collect_components,
    create_account,
    definition_id,
    detector_instances,
)
from atropos_ui.set_runtime import SetRuntime
from streamlit.testing.v1 import AppTest

BASE = 1_790_000_000_000_000_000
VM = Datasource('vm', 'VM', 'http://vm', is_default=True, min_interval_seconds=1)
INFLUX = Datasource('in', 'Influx', 'http://in', 'InfluxDB', bucket='bucket', organization='org')


class MemorySetAccount(AccountStore):
    def __init__(self, workspace=None, username='alice'):
        self._dsn, self.username = 'test', username
        self.workspace, self.revision, self.results = copy.deepcopy(workspace or {}), 1, {}

    def snapshot(self):
        return copy.deepcopy(self.workspace), self.revision

    def _edit(self, change, *, expected_revision=None):
        if expected_revision is not None and expected_revision != self.revision:
            raise ValueError('Настройки изменились.')
        updated = copy.deepcopy(self.workspace)
        result = change(updated)
        self.workspace, self.revision = updated, self.revision + 1
        return result

    def save_set_result(self, set_id, result, *, expected_revision):
        assert expected_revision == self.revision
        self.results[set_id] = copy.deepcopy(result)

    def load_set_result(self, set_id):
        return copy.deepcopy(self.results.get(set_id))


def setup_store():
    definition = MetricDefinition('cpu_util', 'vm', '100-avg(rate(cpu[5m]))*100', extra_labels={'research': 'r1'})
    uid = definition_id(definition.name)
    item = {'id': 'set', 'name': 'My set', 'start': 'now-5m', 'end': 'now', 'refresh_seconds': 15,
            'members': [{'metric_id': uid, 'detectors': {}, 'peers': []}]}
    store = MemorySetAccount({'sources': [asdict(VM)], 'definitions': [asdict(definition)], 'metric_sets': [item]})
    return store, uid


def fake_reader(source, first, last, metrics, *, step):
    return {m['id']: {**m, 'series': [{'labels': {'host': 'a'},
        'times': [BASE + i * 1_000_000_000 for i in range(30)],
        'values': [float(i % 7) for i in range(30)]}]} for m in metrics}, 0


def finish(instance):
    for state in list(instance.jobs.values()):
        state['future'].result(timeout=15)


def test_prometheus_reader_uses_only_saved_queries_and_window():
    store, uid = setup_store()
    metrics = catalog_metrics(store.workspace)

    def handler(request):
        assert request.url.path == '/api/v1/query_range'
        assert request.url.params['query'] == metrics[0]['query']
        assert request.url.params['step'] == '5'
        return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'matrix', 'result': [
            {'metric': {'__name__': 'raw', 'job': 'windows'},
             'values': [['1790000000.123456789', '12'], ['1790000001', 'NaN'], ['1790000002', '13']]}
        ]}})

    raw, skipped = read_metric_window(VM, BASE, BASE + 2_000_000_000, metrics, step=5,
                                      transport=httpx.MockTransport(handler))
    assert skipped == 1
    assert raw[uid]['series'][0] == {'labels': {'job': 'windows'}, 'times': [BASE + 123456789], 'values': [12.]}


def test_set_saves_source_only_series_then_applies_multiple_instances_without_source_access():
    store, uid = setup_store()
    calls = []
    def reader(*args, **kwargs):
        calls.append(args[0].id)
        return fake_reader(*args, **kwargs)
    instance = SetRuntime(reader=reader)
    try:
        assert instance.start(store, 'set', BASE, BASE + 30_000_000_000, 1)
        finish(instance)
        assert instance.state(store, 'set')['error'] is None
        assert calls == ['vm']
        first = instance.result(store, 'set')
        assert not first['metrics'][uid]['detectors']
        assert json.loads(first['metrics'][uid]['series'][0]['label'])['research'] == 'r1'
        item = copy.deepcopy(store.metric_sets()[0])
        item['members'][0]['detectors'] = {
            'short': {'kind': 'MAD', 'name': 'cpu_MAD_short', 'parameters': {'window': 4}},
            'long': {'kind': 'MAD', 'name': 'cpu_MAD_long', 'parameters': {'window': 8}},
        }
        store.save_metric_set(item, expected_revision=store.revision)
        assert instance.start(store, 'set', 0, 1, 1, 'saved')
        finish(instance)
        assert calls == ['vm']
        result = instance.result(store, 'set')
        components, _ = collect_components(result)
        assert {c['name'] for c in components} == {'cpu_MAD_short', 'cpu_MAD_long'}
        assert len({c['id'] for c in components}) == 2
        entities = result_entities(result)
        assert len(entities) == 3 and {e['kind'] for e in entities} == {'metric', 'reaction'}
        assert {c['parameters']['window'] for c in components} == {4, 8}
        store.workspace['definitions'][0]['query'] = 'another_query'
        with pytest.raises(ValueError, match='изменились'):
            instance.start(store, 'set', 0, 1, 1, 'saved')
        with pytest.raises(ValueError, match='не найден'):
            instance.result(MemorySetAccount(username='bob'), 'set')
    finally:
        instance.shutdown()


def test_attach_creates_distinct_named_reactions_and_preserves_other_members():
    store, uid = setup_store()
    metrics = store.catalog_metrics()
    item = attach_detectors(store.metric_sets()[0], metrics, [uid], ['MAD'], [])
    item = attach_detectors(item, metrics, [uid], ['MAD'], [])
    instances = detector_instances(item['members'][0]['detectors'])
    assert [b['name'] for b in instances] == ['cpu_util_MAD', 'cpu_util_MAD_2']
    assert len({b['id'] for b in instances}) == 2
    store.save_metric_set(item, expected_revision=store.revision)
    assert len(store.metric_sets()[0]['members'][0]['detectors']) == 2


def test_exports_metrics_and_reactions_under_their_own_names_with_ns_and_labels():
    entities = [{'key': 'm:x', 'name': 'cpu', 'kind': 'metric', 'labels': {'host': 'a b'},
                 'times': [BASE + 123, BASE + 456], 'values': [1., 2.]},
                {'key': 'r:x', 'name': 'cpu_MAD', 'kind': 'reaction', 'labels': {'detector': 'MAD'},
                 'times': [BASE + 123, BASE + 456], 'values': [None, .75]}]
    assert str(BASE + 123) in metric_csv(entities).decode('utf-8-sig')
    matrix = json.loads(metric_json(entities))['data']['result']
    assert matrix[1]['metric']['__name__'] == 'cpu_MAD'
    assert matrix[0]['values'][0][0] == '1790000000.000000123'
    assert matrix[1]['values'][0][1] == 'NaN'
    writes = []
    def handler(request):
        assert request.url.path == '/api/v2/write' and request.url.params['precision'] == 'ns'
        writes.append(request.content.decode())
        return httpx.Response(204)
    assert write_metric_influx(INFLUX, entities, transport=httpx.MockTransport(handler)) == 3
    assert 'cpu,host=a\\ b value=1.0 ' + str(BASE + 123) in writes[0]
    assert 'cpu_MAD,detector=MAD value=0.75 ' + str(BASE + 456) in writes[0]


def widget(items, label):
    return next(item for item in items if item.label == label)


def test_catalog_adds_saved_metric_to_personal_set(monkeypatch):
    store, _ = setup_store()
    store.workspace['metric_sets'] = []
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.catalog import render
render(context())
''', default_timeout=20).run()
    widget(app.radio, 'Раздел каталога').set_value('Сохранённые метрики').run()
    widget(app.text_input, 'Имя нового сета').set_value('Personal CPU')
    widget(app.button, 'Добавить в сет').click().run()
    assert not app.exception
    assert store.metric_sets()[0]['name'] == 'Personal CPU'
    assert store.metric_sets()[0]['members'][0]['detectors'] == {}


def test_detector_page_selects_set_and_attaches_by_regex(monkeypatch):
    store, uid = setup_store()
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    monkeypatch.setattr('atropos_ui.detector_workspace.set_runtime', lambda: SimpleNamespace(
        state=lambda *args: None, result=lambda *args: None))
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.detectors import render
render(context())
''', default_timeout=20).run()
    assert not app.exception
    widget(app.radio, 'Способ выбора').set_value('Регулярное выражение').run()
    widget(app.text_input, 'Регулярное выражение').set_value('cpu_.*').run()
    widget(app.multiselect, 'Детекторы для прикрепления').set_value(['MAD']).run()
    widget(app.button, 'Прикрепить детекторы').click().run()
    assert not app.exception
    assert next(iter(store.metric_sets()[0]['members'][0]['detectors'].values()))['name'] == 'cpu_util_MAD'
    assert store.metric_sets()[0]['members'][0]['metric_id'] == uid


def test_monitoring_selects_owned_set_searches_after_three_characters_and_refreshes(monkeypatch):
    import time

    from atropos_ui.time_ranges import TimeRange

    store, uid = setup_store()
    store.workspace['metric_sets'][0] = attach_detectors(store.metric_sets()[0], store.catalog_metrics(),
                                                       [uid], ['MAD'], [])
    instance = SetRuntime(reader=fake_reader)
    try:
        instance.start(store, 'set', BASE, BASE + 30_000_000_000, 1)
        finish(instance)
        result = instance.result(store, 'set')
    finally:
        instance.shutdown()
    calls = []
    job = None
    class Runtime:
        def start(self, *args):
            nonlocal job
            calls.append(args)
            job = {'running': False, 'started_at': time.time(), 'error': None}
            return True
        def state(self, *args):
            return job
        def result(self, *args):
            return result

    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    monkeypatch.setattr('atropos_ui.set_monitoring.set_runtime', lambda: Runtime())
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.monitoring import render
render(context())
''', default_timeout=20).run()
    assert not app.exception
    assert len(calls) == 1 and calls[0][1] == 'set'
    assert len(app.get('plotly_chart')) == 1
    assert len(app.get('download_button')) == 2
    widget(app.radio, 'Способ выбора').set_value('По имени').run()
    widget(app.text_input, 'Поиск по имени (от 3 символов)').set_value('cp').run()
    assert widget(app.multiselect, 'Метрики и реакции для отображения').options == []
    widget(app.text_input, 'Поиск по имени (от 3 символов)').set_value('cpu').run()
    assert set(widget(app.multiselect, 'Метрики и реакции для отображения').options) == {'cpu_util', 'cpu_util_MAD'}
    assert len(calls) == 1
    app.session_state['monitor_set_window_time_range'] = TimeRange('now-15m', 'now')
    app.run()
    assert not app.exception and len(calls) == 2
    widget(app.checkbox, 'Автообновление').uncheck().run()
    widget(app.button, 'Обновить сейчас').click().run()
    assert not app.exception and len(calls) == 3


def test_postgres_persists_catalog_sets_results_and_blocks_cross_account_access(postgres_test_dsn):
    users = ['set-test-' + uuid4().hex for _ in range(2)]
    stores = []
    for user in users:
        create_account(user, 'pbkdf2_sha256$test', dsn=postgres_test_dsn)
        stores.append(AccountStore(user, postgres_test_dsn))
    alice, bob = stores
    try:
        alice.save(VM)
        definition = MetricDefinition('cpu_util', 'vm', 'up')
        alice.save_definition(definition)
        _, uid = setup_store()
        item = setup_store()[0].metric_sets()[0]
        alice.save_metric_set(item, expected_revision=alice.snapshot()[1])
        result = {'metrics': {}, 'bounds_ns': [BASE, BASE + 10], 'revision': alice.snapshot()[1]}
        alice.save_set_result('set', result, expected_revision=alice.snapshot()[1])
        reopened = AccountStore(users[0], postgres_test_dsn)
        assert reopened.definitions() == [definition]
        assert reopened.metric_sets()[0]['members'][0]['metric_id'] == uid
        assert reopened.load_set_result('set') == result
        assert not bob.definitions() and not bob.metric_sets() and bob.load_set_result('set') is None
        with pytest.raises(ValueError, match='не принадлежит'):
            bob.save_metric_set(item, expected_revision=bob.snapshot()[1])
        with pytest.raises(ValueError, match='используется'):
            alice.delete('definition', 'cpu_util')
        alice.save_definition(MetricDefinition('renamed', 'vm', 'up'), previous_name='cpu_util')
        assert alice.metric_sets()[0]['members'][0]['metric_id'] == definition_id('renamed')
        with pytest.raises(ValueError, match='изменились'):
            alice.save_set_result('set', result, expected_revision=result['revision'])
    finally:
        with alice._connection() as db:
            db.execute('DELETE FROM atropos_config.accounts WHERE username = ANY(%s)', (users,))


def test_set_http_api_preserves_instance_results_and_refuses_foreign_set(monkeypatch):
    from threading import Thread

    from atropos_ui.runtime_client import RemoteSetRuntime
    from atropos_ui.runtime_server import RuntimeHTTPServer, RuntimeService

    store, uid = setup_store()
    item = attach_detectors(store.metric_sets()[0], store.catalog_metrics(), [uid], ['MAD'], [])
    store.save_metric_set(item, expected_revision=store.revision)
    bob = MemorySetAccount(username='bob')
    token = 'test-set-token-' + 'a' * 40
    monkeypatch.setenv('ATROPOS_RUNTIME_TOKEN', token)
    monkeypatch.delenv('ATROPOS_RUNTIME_TOKEN_FILE', raising=False)
    service = RuntimeService(sets=SetRuntime(reader=fake_reader),
        store_factory=lambda username: {'alice': store, 'bob': bob}[username], health_check=lambda: True)
    server = RuntimeHTTPServer(('127.0.0.1', 0), service, token)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = RemoteSetRuntime(f'http://127.0.0.1:{server.server_port}')
    try:
        assert client.start(store, 'set', BASE, BASE + 30_000_000_000, 1)
        finish(service.sets)
        assert not client.state(store, 'set')['running']
        result = client.result(store, 'set')
        assert len(result_entities(result)) == 2
        assert client.start(store, 'set', 0, 1, 1, 'saved')
        finish(service.sets)
        assert client.result(store, 'set')['execution_mode'] == 'saved_series'
        from atropos_ui.clients import ApiError
        with pytest.raises(ApiError, match='400'):
            client.result(bob, 'set')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        service.shutdown()
