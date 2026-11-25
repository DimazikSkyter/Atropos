import copy
import math
from dataclasses import asdict
from threading import Event

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from atropos_ui.clients import ApiError, HttpApi
from atropos_ui.detector_runtime import (
    ReplayRuntime,
    correlation,
    defaults,
    detector,
    replay,
    unary,
)
from atropos_ui.influx import descriptors, parse_flux
from atropos_ui.live_runtime import LiveHistory, LiveRuntime
from atropos_ui.service_api import AccountStore, Datasource
from atropos_ui.streaming import DetectorStreams

BASE = 1_790_000_000_123456789
SOURCE = Datasource('in', 'Influx', 'http://unused', 'InfluxDB', organization='org', bucket='metrics')


class MemoryAccount(AccountStore):
    def __init__(self, workspace=None, username='alice'):
        self._dsn, self.username = 'unused', username
        self.workspace, self.revision = copy.deepcopy(workspace or {}), 1

    def snapshot(self):
        return copy.deepcopy(self.workspace), self.revision

    def _edit(self, change, *, expected_revision=None):
        if expected_revision is not None and expected_revision != self.revision:
            raise ValueError('revision changed')
        candidate = copy.deepcopy(self.workspace)
        result = change(candidate)
        self.workspace, self.revision = candidate, self.revision + 1
        return result

    def save_results(self, result, revision):
        return False

    def load_results(self):
        return None


def data(count=80):
    series = {'labels': {'host': 'a'}, 'times': [BASE + i * 1_000_000_000 for i in range(count)],
              'values': [math.sin(i / 4) + (i > 40) * 2 for i in range(count)]}
    return {'m': {'id': 'm', 'datasource_id': 'in', 'measurement': 'cpu', 'field': 'value',
                  'name': 'CPU', 'series': [series]}}


def account(raw=None, names=()):
    return MemoryAccount({'sources': [asdict(SOURCE)], 'metrics': descriptors(raw or data(), names)})


@pytest.mark.parametrize('stamp', ['', 'NaT', 'not-a-date'])
def test_bad_timestamp_is_rejected(stamp):
    with pytest.raises(ApiError, match='время'):
        parse_flux(',result,table,_time,_value,_field,_measurement\n,,0,' + stamp + ',1,value,cpu', 'in')


@pytest.mark.parametrize('change', [
    {'name': ''}, {'name': 1}, {'extra_labels': {'team': 1}}, {'extra_labels': []},
    {'drop_labels': 'host'}, {'drop_labels': ['host', 'host']},
    {'detectors': {'Unknown': {}}}, {'detectors': {'MAD': {'window': 0}}},
    {'detectors': {'MAD': {'unknown': 1}}}, {'detectors': {'MAD': {'window': float('nan')}}},
    {'peers': ['foreign']}, {'peers': 'm'},
])
def test_repository_rejects_invalid_edit_atomically(change):
    store = account()
    before = store.snapshot()
    with pytest.raises((ValueError, TypeError)):
        store.update_metric('m', change, expected_revision=store.revision)
    assert store.snapshot() == before


def test_import_rejects_invalid_last_metric_without_partial_write():
    store = account()
    before = store.snapshot()
    good = descriptors(data(), ['MAD'])[0]
    bad = {**good, 'id': 'bad', 'detectors': {'MAD': {'window': -1}}}
    with pytest.raises(ValueError):
        store.import_metrics([good, bad], expected_revision=store.revision)
    assert store.snapshot() == before


def test_run_metadata_keeps_defaults_and_overrides_after_edit(monkeypatch):
    raw = data()
    store = account(raw, ['MAD'])
    store.workspace['metrics'][0]['detectors'] = {'MAD': {'window': 4}}
    result = replay(store, reader=lambda _: (raw, 0))
    metadata = result['metrics']['m']['detectors']['MAD']['metadata']
    assert metadata['requested_parameters'] == {'window': 4}
    assert metadata['effective_parameters']['data_max_size'] == defaults('MAD')['data_max_size']
    assert metadata['effective_parameters']['window'] == 4
    assert len(metadata['implementation_sha256']) == 64 and metadata['is_probability'] is False
    store.update_metric('m', {'detectors': {'MAD': {'window': 8}}}, expected_revision=store.revision)
    assert metadata['effective_parameters']['window'] == 4
    monkeypatch.setattr('atropos_ui.pages.detectors.runtime', lambda: type('Runtime', (), {'result': lambda *a: result})())
    monkeypatch.setattr('atropos_ui.run_info.st', __import__('streamlit'))
    app = AppTest.from_string('''
from atropos_ui.run_info import metadata_panel
from streamlit import session_state
metadata_panel(session_state['result'])
''')
    app.session_state['result'] = result
    app.run()
    assert not app.exception and any('effective_parameters' in j.value for j in app.json)


def test_chart_and_metadata_use_one_result_snapshot(monkeypatch):
    raw, calls = data(20), []
    store = account(raw, ['MAD'])
    result = replay(store, reader=lambda _: (raw, 0))
    class Runtime:
        def result(self, store):
            calls.append(True)
            return result
    monkeypatch.setattr('atropos_ui.pages.detectors.runtime', Runtime)
    app = AppTest.from_string('''
from atropos_ui.pages.detectors import results_panel
from streamlit import session_state
results_panel(None, session_state['metric'])
''')
    app.session_state['metric'] = store.metrics()[0]
    app.run()
    assert not app.exception and len(calls) == 1
    assert [tab.label for tab in app.tabs] == ['Результаты', 'Параметры запуска']


@pytest.mark.parametrize('name,parameters', [
    ('Trend', {'window': 8, 'data_max_size': 32, 'inertia_window': 4}),
    ('MAD', {'window': 4, 'data_max_size': 16}),
    ('CUSUM', {'window': 8, 'reference_window': 32, 'min_reference_samples': 8}),
    ('SSA', {'window': 8, 'data_max_size': 24, 'lag': 4}),
    ('ACF', {'window': 8, 'max_lag': 2, 'reference_window': 16}),
    ('PredictionInterval', {'window': 8}),
    ('RuntimeTrend', {'window_seconds': 8, 'min_samples': 4}),
])
def test_incremental_unary_matches_single_causal_stream_with_overlap(name, parameters):
    series = data()['m']['series'][0]
    series['source'] = {'source_series_id': 'm-a'}
    expected = unary(name, parameters, series)
    streams = DetectorStreams(detector)
    outputs = []
    for first, last in [(0, 30), (20, 50), (40, 80)]:
        part = {**series, 'times': series['times'][first:last], 'values': series['values'][first:last]}
        calculated = unary(name, parameters, part, streams=streams)
        assert calculated['scores'] == pytest.approx(expected['scores'][first:last], nan_ok=True)
        outputs.append(calculated)
    assert streams.states and len(next(iter(streams.states.values()))['records']) == 80
    assert outputs[-1]['status'] == 'ok'


@pytest.mark.parametrize('parameters', [
    {'window': 8, 'max_lag': 1, 'reference_window': 16, 'alignment_tolerance': 1},
    {'window': 8, 'max_lag': 1, 'reference_window': 16, 'adaptive': False},
])
def test_incremental_correlation_matches_full_replay_including_delayed_target(parameters):
    values = data()['m']['series'][0]
    left = {**values, 'label': 'left', 'source': {'source_series_id': 'left'}}
    right = {**values, 'values': [2*v + 0.01 for v in values['values']], 'label': 'right',
             'source': {'source_series_id': 'right'}}
    expected = correlation(parameters, [left, right])['series'][0]
    streams, merged = DetectorStreams(detector), {}
    for first, last in [(0, 30), (30, 50), (50, 80)]:
        part = [{**s, 'times': s['times'][first:last], 'values': s['values'][first:last]} for s in [left, right]]
        output = correlation(parameters, part, streams=streams)['series'][0]
        merged.update(zip(output['times'], output['scores']))
    assert [merged[t] for t in expected['times']] == pytest.approx(expected['scores'], nan_ok=True)


def test_stream_cache_budget_and_correction_detection():
    streams = DetectorStreams(detector, max_points=5, max_streams=1)
    streams.calculate('MAD', {}, ['a'], list(range(10)), list(range(10)))
    assert len(streams.order) == 5
    before = copy.deepcopy(next(iter(streams.states.values()))['records'])
    with pytest.raises(ValueError, match='изменилась'):
        streams.calculate('MAD', {}, ['a'], [9, 10], [999, 10])
    assert next(iter(streams.states.values()))['records'] == before
    with pytest.raises(ValueError, match='MAX_STREAMS'):
        streams.calculate('MAD', {}, ['b'], [10], [10])


def test_model_budget_rejects_before_publishing_a_stream(monkeypatch):
    monkeypatch.setenv('ATROPOS_LIVE_MODEL_POINTS', '1')
    streams = DetectorStreams(detector)
    with pytest.raises(ValueError, match='MODEL_POINTS'):
        streams.calculate('SSA', {}, ['a'], [BASE], [1.0])
    assert not streams.states and streams.model_points == 0


def test_json_response_keeps_bom_decoding_support():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b'\xef\xbb\xbf{"ok":true}'))
    assert HttpApi('http://unused', transport=transport).request('GET', '/') == {'ok': True}


def test_response_byte_and_parser_point_budgets_fail_explicitly():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text='x' * 100))
    with pytest.raises(ApiError, match='MAX_BYTES'):
        HttpApi('http://unused', transport=transport).request('GET', '/', raw=True, max_response_bytes=20)
    csv = ',result,table,_time,_value,_field,_measurement,host\n'
    csv += ',,0,2026-01-01T00:00:00Z,1,value,cpu,a\n,,0,2026-01-01T00:00:01Z,2,value,cpu,b\n'
    for limits in ({'max_points': 1}, {'max_series': 1}):
        with pytest.raises(ApiError, match='неполный импорт'):
            parse_flux(csv, 'in', **limits)


def test_history_retention_and_point_budget_preserve_consistent_arrays():
    raw = data(20)
    result = replay(account(raw, ['MAD']), reader=lambda _: (raw, 0))
    history = LiveHistory(max_runs=2, max_points=25)
    for _ in range(5):
        history.append(result, (BASE, BASE + 20_000_000_000))
    assert len(history.runs) == 2 and history.evicted_runs == 3
    assert sum(len(s['points']) for s in history.sources.values()) + history.reaction_points <= 25
    for run in history.runs:
        for component in run['components']:
            assert len(component['times']) == len(component['scores'])
    assert history.runs[-1]['metadata']['metrics']['m']['detectors']['MAD']['metadata']


def test_live_warms_across_short_windows_and_metadata_is_account_owned(monkeypatch):
    raw = data(40)
    store = account(raw)
    store.workspace['metric_sets'] = [{'id': 'set', 'name': 'set', 'start': 'now-10s', 'end': 'now',
        'refresh_seconds': 10, 'members': [{'metric_id': 'm', 'peers': [],
            'detectors': {'MAD': {'window': 4, 'data_max_size': 16}}}]}]
    now = [BASE / 1e9 + 10]
    instance = LiveRuntime(autostart=False, clock=lambda: now[0], reader=lambda *a: (raw, 0))
    try:
        instance.start(store, 'set')
        for _ in range(3):
            instance.tick()
            session = next(iter(instance.sessions.values()))
            session['future'].result(timeout=10)
            now[0] += 10
        options = instance.component_options(store, session['id'])
        points = instance.snapshot(store, session['id'], [options[0]['id']])['components'][0]
        assert any(v is not None for v in points['scores'])  # Each window alone has only ten samples.
        meta = instance.metadata(store, session['id'])
        assert meta['execution_mode'] == 'incremental_live'
        assert meta['metrics']['m']['detectors']['MAD']['metadata']['effective_parameters']['window'] == 4
        with pytest.raises(ValueError, match='не принадлежит'):
            instance.metadata(MemoryAccount(username='bob'), session['id'])
        instance.stop(store, session['id'])
        instance.delete(store, session['id'])
        assert not instance.sessions
    finally:
        instance.shutdown()


def test_runtime_trend_keeps_adjacent_nanoseconds_and_same_semantics():
    from atropos_detection.runtime_trend import RuntimeTrendDetector, TrendConfig
    ns = RuntimeTrendDetector(TrendConfig(7e-9, 1, 0, min_samples=4, min_coverage=1))
    seconds = RuntimeTrendDetector(ns.config)
    for i in range(8):
        ns.update_ns(BASE + i, float(i))
        seconds.update(i / 1e9, float(i))
    result = ns.evaluate()
    assert result.ready and result.timestamp_ns == BASE + 7
    assert result.tangent == pytest.approx(seconds.evaluate().tangent)
    with pytest.raises(ValueError, match='mix'):
        ns.update(1.0, 1.0)


def test_replay_queue_is_bounded_without_cancelling_inflight_jobs(monkeypatch):
    entered, release = Event(), Event()
    def calculate(store, progress):
        entered.set()
        assert release.wait(10)
        return {'revision': store.revision}
    monkeypatch.setattr('atropos_ui.detector_runtime.replay', calculate)
    monkeypatch.setenv('ATROPOS_REPLAY_MAX_ACCOUNTS', '1')
    instance = ReplayRuntime()
    try:
        instance.start(MemoryAccount(username='alice'))
        assert entered.wait(5)
        with pytest.raises(ValueError, match='заполнена'):
            instance.start(MemoryAccount(username='bob'))
        assert len(instance.jobs) == 1
    finally:
        release.set()
        instance.pool.shutdown(wait=True)


def test_live_session_limit_and_expiration(monkeypatch):
    monkeypatch.setenv('ATROPOS_LIVE_MAX_SESSIONS', '1')
    monkeypatch.setenv('ATROPOS_LIVE_RETENTION_SECONDS', '10')
    store = account()
    store.workspace['metric_sets'] = [{'id': 'a', 'name': 'A', 'start': 'now-10s', 'end': 'now',
        'refresh_seconds': 10, 'members': [{'metric_id': 'm', 'peers': [], 'detectors': {'MAD': {}}}]}]
    now = [BASE / 1e9]
    instance = LiveRuntime(autostart=False, clock=lambda: now[0])
    try:
        instance.start(store, 'a')
        bob = MemoryAccount(store.workspace, username='bob')
        with pytest.raises(ValueError, match='лимит'):
            instance.start(bob, 'a')
        uid = next(iter(instance.sessions))
        instance.stop(store, uid)
        now[0] += 11
        instance.tick()
        assert not instance.sessions and not instance.active
    finally:
        instance.shutdown()


def test_launcher_initializes_runtime_before_starting_ui(monkeypatch):
    import sys

    from atropos_ui.serve import main
    events = []
    monkeypatch.delenv('ATROPOS_UI_DEMO', raising=False)
    monkeypatch.setattr('atropos_ui.replay_ui.runtime', lambda: events.append('runtime'))
    monkeypatch.setattr('streamlit.web.cli.main', lambda: events.append('ui'))
    monkeypatch.setattr(sys, 'argv', ['atropos-ui'])
    main()
    assert events == ['runtime', 'ui']


def test_postgres_rejects_invalid_edit_without_revision_or_result_change(postgres_test_dsn):
    from uuid import uuid4

    from atropos_ui.service_api import create_account
    name = 'integrity-' + uuid4().hex
    create_account(name, 'pbkdf2_sha256$test', dsn=postgres_test_dsn)
    store = AccountStore(name, postgres_test_dsn)
    store.save(SOURCE)
    store.import_metrics(descriptors(data(), ['MAD']), expected_revision=store.snapshot()[1])
    store.save_preferences({**store.preferences(), 'persist_results': True})
    result = replay(store, reader=lambda _: (data(), 0))
    before = store.snapshot()
    with pytest.raises(ValueError):
        store.update_metric('m', {'detectors': {'MAD': {'window': 0}}}, expected_revision=before[1])
    assert store.snapshot() == before and store.load_results() == result
