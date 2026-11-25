import copy
import csv
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from atropos_ui.clients import ApiError
from atropos_ui.detector_runtime import replay
from atropos_ui.influx import descriptors
from atropos_ui.reaction_export import csv_export, line_protocol, write_influx
from atropos_ui.service_api import (
    AccountStore,
    Datasource,
    aggregate_components,
    collect_components,
    select_components,
)

TIMES = [1_780_000_000_123456789 + n * 15_000_000_000 for n in range(3)]


def source(tags, values=None):
    return {'labels': tags, 'label': json.dumps(tags, sort_keys=True), 'times': TIMES,
            'values': values or [10, 11, 20]}


@pytest.fixture
def result():
    def metric(name, series, scores):
        return {'name': name, 'status': 'ok', 'series': series, 'detectors': {
            'MAD': {'status': 'ok', 'series': [{'scores': values, 'label': s['label']}
                                             for s, values in zip(series, scores)]}}}
    return {'revision': 7, 'finished_at': 1_780_000_050, 'warnings': [], 'metrics': {
        'a': metric('Requests', [source({'service': 'orders'}), source({'service': 'billing'})],
                    [[None, 0.4, 0.5], [None, 0.1, 0.2]]),
        'b': metric('Latency', [source({'service': 'orders'}), source({'host': 'db'}), source({'service': ''})],
                    [[None, 0.3, 0.2], [None, 0.8, 0.9], [None, 0.05, 0.1]])}}


def test_filters_match_effective_labels_and_distinguish_missing_empty_and_fullmatch(result):
    series = result['metrics']['a']['series'][0]
    series['label'] = json.dumps({'service': 'orders', 'team': 'checkout'})
    components, _ = collect_components(result)
    assert len(select_components(components, label_regex={'service': '.+'})) == 3
    assert len(select_components(components, label_regex={'service': '.*'})) == 4
    assert len(select_components(components, labels={'service': ''})) == 1
    assert len(select_components(components, labels={'team': 'checkout'}, label_regex={'service': 'ord.*'})) == 1
    assert select_components(components, label_regex={'service': 'order'}) == []
    assert select_components(components, detectors=[]) == []
    for invalid in ([], '', 1):
        with pytest.raises(ValueError):
            select_components([], labels=invalid)
    with pytest.raises(ValueError, match='регулярное'):
        select_components([], label_regex={'service': '['})


def test_sums_all_metrics_by_label_keep_gaps_and_provenance(result):
    components, _ = collect_components(result)
    chosen = select_components(components, label_regex={'service': '.+'})
    groups = aggregate_components(chosen, ['service'])
    orders = next(g for g in groups if g['name'] == 'service=orders')
    assert orders['scores'] == [None, 0.7, 0.7]
    assert orders['coverage'] == [0, 2, 2]
    assert orders['times'] == TIMES
    assert len(orders['components']) == 2
    assert len({c['source_series_id'] for c in components}) == 5
    assert {c['metric_id'] for c in chosen} == {'a', 'b'}
    before = chosen[0]['source_series_id']
    result['metrics']['a']['name'] = 'Renamed'
    result['metrics']['a']['series'][0]['label'] = '{"team": "other"}'
    assert collect_components(result)[0][0]['source_series_id'] == before


def test_exact_timestamp_alignment_never_interpolates_or_reuses_previous_score(result):
    result['metrics']['b']['series'][0]['times'] = [t + 1 for t in TIMES]
    chosen = select_components(collect_components(result)[0], labels={'service': 'orders'})
    group = aggregate_components(chosen, [])[0]
    assert group['times'] == sorted(TIMES + [t + 1 for t in TIMES])
    assert group['scores'] == [None, None, 0.4, 0.3, 0.5, 0.2]
    assert group['coverage'] == [0, 0, 1, 1, 1, 1]


def test_correlation_pairs_are_traceable_filtered_on_both_members_and_deduplicated(result):
    components, _ = collect_components(result)
    left, right = components[0]['sources'][0], components[2]['sources'][0]
    pair = {'scores': [None, 0.6, 0.7], 'times': TIMES, 'label': 'pair', 'sources': [left, right]}
    for metric in result['metrics'].values():
        metric['detectors']['Correlation'] = {'parameters': {'window': 4}, 'series': [copy.deepcopy(pair)]}
    components, _ = collect_components(result)
    pairs = select_components(components, detectors=['Correlation'], labels={'service': 'orders'})
    assert len(pairs) == 1 and len(pairs[0]['sources']) == 2
    assert len(select_components(components, detectors=['Correlation'], labels={'service': 'billing'})) == 0
    result['metrics']['b']['detectors']['Correlation']['parameters']['window'] = 5
    assert len(select_components(collect_components(result)[0], detectors=['Correlation'])) == 2


def test_csv_preserves_ns_warmup_and_component_metadata(result):
    components, _ = collect_components(result)
    groups = aggregate_components(components, ['service'])
    rows = list(csv.DictReader(io.StringIO(csv_export(groups, components).decode('utf-8-sig'))))
    component = next(r for r in rows if r['kind'] == 'component')
    assert component['timestamp_ns'] == str(TIMES[0])
    assert '.123456789' in component['time']
    assert component['score'] == '' and component['coverage'] == '0'
    assert component['detector'] == 'MAD' and component['metric_id'] == 'a'
    assert json.loads(component['labels'])['sources'][0]['source_labels'] == {'service': 'orders'}


def test_influx_batches_authorization_prefix_escaping_and_partial_failures(result, monkeypatch):
    components, _ = collect_components(result)
    components[0]['sources'][0]['labels'] = {'service': 'a,b=c d\\e', 'detector': 'user tag'}
    components = components[:1]
    groups = aggregate_components(components, ['service'])
    lines = list(line_protocol(groups, components, 'reaction'))
    assert len(lines) == 4
    assert any('detector=MAD' in line and r'service=a\,b\=c\ d\\e' in line for line in lines)
    assert any(r'label.detector=user\ tag' in line for line in lines)
    monkeypatch.setenv('REACTION_TEST_TOKEN', 'secret')
    target = Datasource('out', 'Out', 'http://influx/prefix', 'InfluxDB',
        token_env='REACTION_TEST_TOKEN', organization='org', bucket='out')
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.path == '/prefix/api/v2/write'
        assert dict(request.url.params) == {'org': 'org', 'bucket': 'out', 'precision': 'ns'}
        assert request.headers['Authorization'] == 'Token secret'
        return httpx.Response(204)
    assert write_influx(target, groups, components, 'reaction', transport=httpx.MockTransport(handler), batch_size=2) == 4
    assert len(requests) == 2
    calls = []
    def failure(request):
        calls.append(request)
        return httpx.Response(204 if len(calls) == 1 else 403, text='secret body')
    with pytest.raises(ApiError, match='подтверждено точек: 2') as error:
        write_influx(target, groups, components, 'reaction', transport=httpx.MockTransport(failure), batch_size=2)
    assert 'secret' not in str(error.value)
    with pytest.raises(ValueError):
        list(line_protocol(groups, components, 'bad\nname'))


class MemoryAccount(AccountStore):
    def __init__(self):
        self.username, self._dsn = 'monitor-test', 'test'
        self.workspace = {'metrics': [], 'sources': []}

    def snapshot(self):
        return self.workspace, 7

    def sources(self):
        return []

    def save_results(self, result, revision):
        self.saved = result


def test_replay_adds_original_series_ids_for_unary_and_correlation():
    store = MemoryAccount()
    raw = {'a': {'id': 'a', 'datasource_id': 'in', 'measurement': 'cpu', 'field': 'value', 'name': 'CPU',
                 'series': [source({'host': 'a'}), source({'host': 'b'})]}}
    store.workspace = {'metrics': descriptors(raw, ['MAD', 'Correlation']), 'sources': [{
        'id': 'in', 'name': 'In', 'url': 'http://influx', 'kind': 'InfluxDB', 'organization': 'org', 'bucket': 'b'}]}
    store.workspace['metrics'][0]['extra_labels'] = {'team': 'checkout'}
    output = replay(store, reader=lambda s: (raw, 0))
    components, warnings = collect_components(output)
    assert not warnings
    assert len(components) == 3
    pair = next(c for c in components if c['detector'] == 'Correlation')
    assert len(pair['sources']) == 2
    assert all(s['labels']['team'] == 'checkout' and 'team' not in s['source_labels'] for s in pair['sources'])
    assert all(s['datasource_id'] == 'in' and s['measurement'] == 'cpu' and s['field'] == 'value' for s in pair['sources'])


def test_export_sum_identity_separates_different_selections(result):
    components, _ = collect_components(result)
    chosen = select_components(components, labels={'service': 'orders'})
    full = aggregate_components(chosen, ['service'])[0]
    partial = aggregate_components(chosen[:1], ['service'])[0]
    assert full['id'] != partial['id']
    assert aggregate_components(list(reversed(chosen)), ['service'])[0]['id'] == full['id']


def test_grouping_keeps_missing_empty_and_mixed_pair_values_distinct(result):
    components, _ = collect_components(result)
    groups = aggregate_components(components, ['service'])
    assert len(groups) == 4
    assert len({g['id'] for g in groups}) == 4
    pair = copy.deepcopy(components[0])
    pair['sources'].append(components[1]['sources'][0])
    assert aggregate_components([pair], ['service'])[0]['name'] == 'service=billing ↔ orders'


def test_influx_validates_all_points_before_writing_and_handles_warmup(result):
    components, _ = collect_components(result)
    components[-1]['sources'][0]['labels']['bad'] = 'bad\nvalue'
    target = Datasource('out', 'Out', 'http://influx', 'InfluxDB', organization='org', bucket='out')
    requests = []
    with pytest.raises(ValueError, match='управляющие'):
        write_influx(target, aggregate_components(components, []), components, 'reaction',
                     transport=httpx.MockTransport(lambda request: requests.append(request)))
    assert not requests
    with pytest.raises(ValueError, match='Нет рассчитанных'):
        write_influx(target, [{'id': 'a', 'labels': {}, 'times': TIMES, 'scores': [None] * 3,
                              'coverage': [0] * 3}], [], 'reaction')


def test_monitoring_page_filters_chart_drilldown_and_export_use_same_selection(result, monkeypatch):
    store = MemoryAccount()
    class Runtime:
        def state(self, store):
            return None
        def result(self, store):
            return result
        def start(self, store):
            return True
    monkeypatch.setattr('atropos_ui.pages.monitoring.runtime', lambda: Runtime())
    monkeypatch.setattr('atropos_ui.pages.detectors.runtime', lambda: Runtime())
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.monitoring import legacy_render as render
render(context())
''', default_timeout=15).run()
    assert not app.exception
    chart = json.loads(app.get('plotly_chart')[0].proto.spec)
    assert {t['name'] for t in chart['data']} == {'service=orders', 'service=billing'}
    assert all(t['stackgroup'] == 'reactions' for t in chart['data'])
    assert len(app.get('download_button')) == 1
    assert len(app.get('plotly_chart')) == 3
    next(t for t in app.text_input if t.label == 'Регулярное выражение значения').set_value('orders').run()
    assert not app.exception
    assert len(json.loads(app.get('plotly_chart')[0].proto.spec)['data']) == 1
    next(t for t in app.text_input if t.label == 'Регулярное выражение значения').set_value('[').run()
    assert not app.exception and app.error
    assert not app.get('plotly_chart') and not app.get('download_button')
    next(t for t in app.text_input if t.label == 'Регулярное выражение значения').set_value('missing').run()
    assert not app.exception and any('Нет реакций' in i.value for i in app.info)


def test_monitoring_rejects_stale_results(result, monkeypatch):
    store = MemoryAccount()
    result['revision'] = 6
    monkeypatch.setattr('atropos_ui.pages.monitoring.runtime', lambda: SimpleNamespace(result=lambda store: result))
    monkeypatch.setattr('atropos_ui.pages.monitoring.progress_panel', lambda store: None)
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.monitoring import legacy_render as render
render(context())
''').run()
    assert not app.exception and not app.get('download_button')
    assert any('Настройки изменились' in i.value for i in app.info)
