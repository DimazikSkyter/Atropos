import copy
import json
from dataclasses import asdict
from threading import Event

import httpx
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from atropos_ui.influx import descriptors, metric_id, read_window
from atropos_ui.live_runtime import LiveHistory, LiveRuntime, prepare_set, window_bounds
from atropos_ui.service_api import (
    AccountStore,
    Datasource,
    create_account,
    normalize_time,
    validate_metric_set,
)

BASE = int(pd.Timestamp('2026-11-18T09:00:00Z').value)
SOURCE = Datasource('in', 'Influx', 'http://influx', 'InfluxDB', organization='org', bucket='metrics')


class MemoryAccount(AccountStore):
    def __init__(self, workspace=None, username='alice'):
        self._dsn, self.username = 'test', username
        self.workspace, self.revision = copy.deepcopy(workspace or {}), 1

    def snapshot(self):
        return copy.deepcopy(self.workspace), self.revision

    def _edit(self, change, *, expected_revision=None):
        if expected_revision is not None and expected_revision != self.revision:
            raise ValueError('Настройки изменились.')
        workspace = copy.deepcopy(self.workspace)
        result = change(workspace)
        self.workspace, self.revision = workspace, self.revision + 1
        return result


def raw_metric(uid, name, *, offset=0, count=30):
    return {'id': uid, 'datasource_id': 'in', 'measurement': name, 'field': 'value', 'name': name,
            'series': [{'labels': {'host': 'a'}, 'times': [BASE + i * 15_000_000_000 for i in range(count)],
                        'values': [float(i + offset) for i in range(count)]}]}


def metric_set(uid='set', **changes):
    return {'id': uid, 'name': uid, 'start': 'now() - 5m', 'end': 'now()', 'refresh_seconds': 15,
            'members': [{'metric_id': 'a', 'detectors': {'MAD': {'window': 4}}, 'peers': []}], **changes}


@pytest.fixture
def setup():
    raw = {'a': raw_metric('a', 'CPU'), 'b': raw_metric('b', 'Memory', offset=2)}
    workspace = {'sources': [asdict(SOURCE)], 'metrics': descriptors(raw, ['Trend']),
                 'metric_sets': [metric_set()]}
    return MemoryAccount(workspace), raw


@pytest.fixture
def runtime_factory():
    instances = []
    def make(**kwargs):
        instance = LiveRuntime(autostart=False, **kwargs)
        instances.append(instance)
        return instance
    yield make
    for instance in instances:
        instance.shutdown()


def finish(instance):
    for session in list(instance.sessions.values()):
        if session['future'] is not None:
            session['future'].result(timeout=15)


def test_sets_validate_ownership_parameters_and_relative_syntax(setup):
    store, _ = setup
    assert normalize_time('now() - 5m') == 'now-5m'
    assert normalize_time('now()') == 'now'
    for start in ['2026-01-01', 'now(); print(1)', 'yesterday']:
        with pytest.raises(ValueError):
            validate_metric_set(metric_set(start=start), store.metrics())
    for interval in [0, -1, True, float('nan'), float('inf'), '15']:
        with pytest.raises(ValueError):
            validate_metric_set(metric_set(refresh_seconds=interval), store.metrics())
    for members in [[], [{'metric_id': 'foreign', 'detectors': {'MAD': {}}, 'peers': []}],
                    [{'metric_id': 'a', 'detectors': {'unknown': {}}, 'peers': []}],
                    [{'metric_id': 'a', 'detectors': {'MAD': {}}, 'peers': ['foreign']}]]:
        with pytest.raises(ValueError):
            store.save_metric_set(metric_set(members=members), expected_revision=store.revision)
    assert validate_metric_set(metric_set(members=[{'metric_id': 'a', 'detectors': {}, 'peers': []}]),
                               store.metrics())['members'][0]['detectors'] == {}
    saved = metric_set(name=' Production ')
    store.save_metric_set(saved, expected_revision=store.revision)
    assert store.metric_sets()[0]['name'] == 'Production'
    with pytest.raises(ValueError, match='изменились'):
        store.save_metric_set(saved, expected_revision=1)
    with pytest.raises(ValueError, match='используется'):
        store.remove_metric('a', expected_revision=store.revision)
    other = MemoryAccount(username='bob')
    with pytest.raises(ValueError, match='не принадлежит'):
        other.save_metric_set(saved, expected_revision=other.revision)


def test_window_uses_one_anchor_and_rejects_inverted_bounds():
    now = BASE / 1e9 + 300
    assert window_bounds(validate_metric_set(metric_set(), [{'id': 'a'}]), now) == (BASE, BASE + 300_000_000_000)
    assert window_bounds(validate_metric_set(metric_set(), [{'id': 'a'}]), now + 15) == (
        BASE + 15_000_000_000, BASE + 315_000_000_000)
    with pytest.raises(ValueError, match='позже'):
        window_bounds(metric_set(start='now', end='now-5m'), now)


def test_read_window_filters_fields_escapes_strings_and_preserves_nanoseconds():
    source = Datasource('in', 'Influx', 'http://influx', 'InfluxDB', organization='org', bucket='a"b')
    metric = {'measurement': 'cpu"x', 'field': 'value\\x'}
    def request(req):
        query = req.content.decode()
        assert 'range(start: time(v: "2026-11-18T09:00:00.123456789+00:00")' in query
        assert 'stop: time(v: "2026-11-18T09:05:00.123456789+00:00")' in query
        assert 'r._measurement == "cpu\\"x"' in query and 'r._field == "value\\\\x"' in query
        assert 'from(bucket: "a\\"b")' in query and 'aggregateWindow' not in query
        return httpx.Response(200, text=',result,table,_time,_value,_field,_measurement,host\n'
            ',,0,2026-11-18T09:00:00.123456789Z,12,value,cpu,a\n')
    raw, skipped = read_window(source, BASE + 123456789, BASE + 300_123456789, [metric],
                               transport=httpx.MockTransport(request))
    assert not skipped
    assert raw[metric_id('in', 'cpu', 'value')]['series'][0]['times'] == [BASE + 123456789]


def test_runtime_recalculates_only_set_bindings_and_accumulates_old_windows(setup, runtime_factory):
    store, raw = setup
    now = [BASE / 1e9 + 300]
    calls = []
    def reader(source, first, last, metrics):
        calls.append((source.id, first, last, [m['id'] for m in metrics]))
        return raw, 0  # Deliberately includes another metric and samples outside the window.
    instance = runtime_factory(reader=reader, clock=lambda: now[0])
    assert instance.start(store, 'set')
    assert not instance.start(store, 'set')
    instance.tick()
    finish(instance)
    session = instance.summaries(store)[0]
    uid = session['id']
    component = instance.component_options(store, uid)[0]
    assert component['detector'] == 'MAD'
    first = instance.snapshot(store, uid, [component['id']], run=0)
    assert first['sources'][0]['times'] == raw['a']['series'][0]['times'][:20]
    assert session['source_points'] == 20 and session['reaction_points'] == 20
    assert calls == [('in', BASE, BASE + 300_000_000_000, ['a'])]
    now[0] += 14
    instance.tick()
    assert len(calls) == 1
    now[0] += 1
    instance.tick()
    finish(instance)
    assert instance.summaries(store)[0]['runs'] == 2
    accumulated = instance.snapshot(store, uid, [component['id']])
    assert len(accumulated['sources'][0]['times']) == 21
    assert accumulated['sources'][0]['times'][0] == BASE
    assert accumulated['components'][0]['times'][0] == BASE
    assert instance.snapshot(store, uid, [component['id']], run=0) == first
    # UI snapshots are copies, so callers cannot mutate the retained history.
    accumulated['components'][0]['scores'][0] = 99
    assert instance.snapshot(store, uid, [component['id']])['components'][0]['scores'][0] != 99
    assert store.revision == 1 and store.workspace['metrics'][0]['detectors'] == {'Trend': {}}


def test_history_keeps_each_recalculation_and_does_not_replace_ready_scores_with_warmup():
    history = LiveHistory()
    def result(times, values, scores):
        return {'finished_at': 1, 'metrics': {'a': {'name': 'A', 'status': 'ok', 'series': [
            {'label': '{"host": "a"}', 'labels': {'host': 'a'}, 'times': times, 'values': values}],
            'detectors': {'MAD': {'series': [{'label': 'a', 'scores': scores}]}}}}}
    history.append(result([1, 2, 3], [10, 20, 30], [None, 0.8, 0.4]), (1, 4))
    history.append(result([2, 3, 4], [20, 30, 40], [None, 0.9, 0.3]), (2, 5))
    uid = next(iter(history.components))
    assert history.snapshot([uid])['components'][0]['scores'] == [None, 0.8, 0.9, 0.3]
    assert history.snapshot([uid], 0)['components'][0]['scores'] == [None, 0.8, 0.4]
    assert history.snapshot([uid], 1)['components'][0]['scores'] == [None, 0.9, 0.3]
    assert history.snapshot([uid], 1)['sources'][0]['times'] == [2, 3, 4]
    assert history.reaction_points == 6


def test_correlation_loads_peers_with_exact_alignment_and_set_specific_parameters(setup, runtime_factory):
    store, raw = setup
    store.workspace['metric_sets'][0]['members'] = [
        {'metric_id': 'a', 'detectors': {'Correlation': {'window': 16}}, 'peers': ['b']}]
    raw['b']['series'][0]['times'] = raw['b']['series'][0]['times'][1:]
    raw['b']['series'][0]['values'] = raw['b']['series'][0]['values'][1:]
    item, plan, _ = prepare_set(store.workspace, 'set')
    assert {m['id']: m['detectors'] for m in plan['metrics']} == {'a': {'Correlation': {'window': 16}}, 'b': {}}
    instance = runtime_factory(reader=lambda *args: (raw, 0), clock=lambda: BASE / 1e9 + 300)
    instance.start(store, 'set')
    instance.tick()
    finish(instance)
    session = instance.summaries(store)[0]
    options = instance.component_options(store, session['id'])
    assert len(options) == 1 and options[0]['detector'] == 'Correlation'
    data = instance.snapshot(store, session['id'], [options[0]['id']])
    assert data['components'][0]['times'] == raw['a']['series'][0]['times'][1:20]
    assert len(data['sources']) == 2


def test_scheduler_bounds_concurrency_skips_overlap_and_resumes_without_losing_history(setup, runtime_factory):
    store, raw = setup
    entered, release = Event(), Event()
    now = [BASE / 1e9 + 300]
    calls = []
    def reader(*args):
        calls.append(args[1:3])
        entered.set()
        assert release.wait(10)
        return raw, 0
    instance = runtime_factory(reader=reader, clock=lambda: now[0], max_workers=1)
    instance.start(store, 'set')
    instance.tick()
    try:
        assert entered.wait(5)
        now[0] += 45
        instance.tick()
        assert len(calls) == 1
        session = instance.summaries(store)[0]
        instance.stop(store, session['id'])
    finally:
        release.set()
    finish(instance)
    instance.tick()
    assert len(calls) == 1 and instance.summaries(store)[0]['runs'] == 1
    instance.start(store, 'set')
    instance.tick()
    finish(instance)
    assert len(instance.summaries(store)) == 1 and instance.summaries(store)[0]['runs'] == 2
    assert calls[-1] == (BASE + 45_000_000_000, BASE + 345_000_000_000)


def test_configuration_changes_archive_history_and_accounts_are_isolated(setup, runtime_factory):
    store, raw = setup
    instance = runtime_factory(reader=lambda *args: (raw, 0), clock=lambda: BASE / 1e9 + 300)
    instance.start(store, 'set')
    instance.tick()
    finish(instance)
    old = instance.summaries(store)[0]
    bob = MemoryAccount(store.workspace, 'bob')
    assert instance.summaries(bob) == []
    with pytest.raises(ValueError, match='не принадлежит'):
        instance.snapshot(bob, old['id'], [])
    store.workspace['preferences'] = {'persist_results': True}
    store.revision += 1
    instance.tick()
    assert instance.summaries(store)[0]['running']
    store.workspace['metric_sets'][0]['members'][0]['detectors']['MAD']['window'] = 5
    store.revision += 1
    instance.tick()
    assert not instance.summaries(store)[0]['running']
    assert instance.summaries(store)[0]['error']
    instance.start(store, 'set')
    instance.tick()
    finish(instance)
    sessions = instance.summaries(store)
    assert len(sessions) == 2 and sessions[0]['runs'] == sessions[1]['runs'] == 1
    assert sessions[0]['id'] == old['id'] and sessions[0]['id'] != sessions[1]['id']


def test_empty_failed_and_recovered_windows_keep_history(setup, runtime_factory):
    store, raw = setup
    now = [BASE / 1e9 + 300]
    responses = [(raw, 0), ({}, 0), RuntimeError('source failed'), (raw, 0)]
    def reader(*args):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    instance = runtime_factory(reader=reader, clock=lambda: now[0])
    instance.start(store, 'set')
    for _ in range(4):
        instance.tick()
        finish(instance)
        now[0] += 15
    session = instance.summaries(store)[0]
    assert session['runs'] == 4 and session['source_points'] == 23
    assert session['running'] and session['components'] == 1
    assert instance.sessions[session['id']]['history'].runs[2]['warnings']


def test_scheduler_runs_without_any_ui_or_ticks(setup):
    store, raw = setup
    entered = Event()
    def reader(*args):
        entered.set()
        return raw, 0
    instance = LiveRuntime(reader=reader, clock=lambda: BASE / 1e9 + 300)
    try:
        instance.start(store, 'set')
        assert entered.wait(5)
        finish(instance)
        assert instance.summaries(store)[0]['runs'] == 1
    finally:
        instance.shutdown()


def test_thousand_combinations_are_evaluated_with_one_source_read(runtime_factory):
    raw = {str(i): raw_metric(str(i), 'metric_' + str(i), count=20) for i in range(1000)}
    members = [{'metric_id': uid, 'detectors': {'MAD': {'window': 4}}, 'peers': []} for uid in raw]
    store = MemoryAccount({'sources': [asdict(SOURCE)], 'metrics': descriptors(raw, []),
                           'metric_sets': [metric_set(members=members)]})
    calls = []
    def reader(*args):
        calls.append(len(args[-1]))
        return raw, 0
    instance = runtime_factory(reader=reader, clock=lambda: BASE / 1e9 + 300)
    instance.start(store, 'set')
    instance.tick()
    finish(instance)
    summary = instance.summaries(store)[0]
    assert calls == [1000] and summary['components'] == summary['source_series'] == 1000
    assert summary['source_points'] == summary['reaction_points'] == 20000
    options = instance.component_options(store, summary['id'])
    selected = instance.snapshot(store, summary['id'], [options[0]['id']])
    assert len(selected['components']) == len(selected['sources']) == 1


def test_live_page_saves_sets_and_only_renders_selected_reactions(setup, runtime_factory, monkeypatch):
    store, raw = setup
    instance = runtime_factory(reader=lambda *args: (raw, 0), clock=lambda: BASE / 1e9 + 300)
    monkeypatch.setattr('atropos_ui.pages.live.live_runtime', lambda: instance)
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.live import render
render(context())
''', default_timeout=20).run()
    assert not app.exception and not app.get('plotly_chart')
    next(t for t in app.text_input if t.label == 'Имя сета').set_value('Runtime set')
    next(s for s in app.multiselect if s.label == 'Метрики сета').set_value(['a', 'b'])
    next(b for b in app.button if b.label == 'Сохранить сет').click().run()
    assert not app.exception and len(store.metric_sets()) == 2
    saved = store.metric_sets()[-1]
    assert saved['start'] == 'now-5m' and saved['refresh_seconds'] == 15
    assert len(saved['members']) == 2 and set(saved['members'][0]['detectors']) == {'MAD', 'CUSUM'}
    next(s for s in app.multiselect if s.label == 'Сеты для запуска').set_value(['set']).run()
    next(b for b in app.button if b.label == 'Запустить выбранные сеты').click().run()
    instance.tick()
    finish(instance)
    app.run()
    assert not app.exception and not app.get('plotly_chart')
    next(c for c in app.checkbox if c.label == 'Показывать графики реакций').check().run()
    assert not app.get('plotly_chart')
    session = instance.summaries(store)[0]
    component_id = instance.component_options(store, session['id'])[0]['id']
    next(s for s in app.multiselect if s.label == 'Реакции для отображения').set_value([component_id]).run()
    assert not app.exception and len(app.get('plotly_chart')) == 1
    chart = json.loads(app.get('plotly_chart')[0].proto.spec)
    assert len(chart['data']) == 2 and chart['data'][1]['connectgaps'] is False
    assert 'MAD' in chart['data'][1]['name']
    next(r for r in app.radio if r.label == 'История реакций').set_value('Отдельное окно').run()
    assert not app.exception
    next(c for c in app.checkbox if c.label == 'Показывать графики реакций').uncheck().run()
    assert not app.exception and not app.get('plotly_chart')
    assert instance.summaries(store)[0]['running']
    next(b for b in app.button if b.label == 'Остановить сессию').click().run()
    assert not app.exception and not instance.summaries(store)[0]['running']


def test_postgres_persists_sets_and_rejects_stale_or_foreign_edits(postgres_test_dsn):
    from uuid import uuid4

    username = 'live-test-' + uuid4().hex
    assert create_account(username, 'pbkdf2_sha256$test', dsn=postgres_test_dsn)
    store = AccountStore(username, postgres_test_dsn)
    store.save(SOURCE)
    raw = {'a': raw_metric('a', 'CPU')}
    store.import_metrics(descriptors(raw, []), expected_revision=store.snapshot()[1])
    revision = store.snapshot()[1]
    store.save_metric_set(metric_set(), expected_revision=revision)
    assert AccountStore(username, postgres_test_dsn).metric_sets()[0]['start'] == 'now-5m'
    with pytest.raises(ValueError, match='изменились'):
        store.remove_metric_set('set', expected_revision=revision)
    with pytest.raises(ValueError, match='не принадлежит'):
        store.save_metric_set(metric_set(members=[{'metric_id': 'foreign', 'detectors': {'MAD': {}}, 'peers': []}]),
                              expected_revision=store.snapshot()[1])
    store.remove_metric_set('set', expected_revision=store.snapshot()[1])
    assert store.metric_sets() == []
