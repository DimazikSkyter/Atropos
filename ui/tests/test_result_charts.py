import json

import pytest
from atropos_ui.set_runtime import SetRuntime
from streamlit.testing.v1 import AppTest
from test_set_workflow import BASE, attach_detectors, finish, setup_store


def test_warmup_is_visible_and_metric_reaction_panels_share_axes(monkeypatch):
    store, uid = setup_store()
    item = attach_detectors(store.metric_sets()[0], store.catalog_metrics(), [uid], ['CUSUM', 'Trend'], [])
    store.save_metric_set(item, expected_revision=store.revision)

    def reader(source, first, last, metrics, *, step):
        return {m['id']: {**m, 'series': [{'labels': {'host': 'a'},
            'times': [BASE + i * 1_000_000_000 for i in range(30)],
            'values': [15.] * 30}]} for m in metrics}, 0

    instance = SetRuntime(reader=reader)
    try:
        instance.start(store, 'set', BASE, BASE + 30_000_000_000, 1)
        finish(instance)
        result = instance.result(store, 'set')
    finally:
        instance.shutdown()
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    monkeypatch.setattr('atropos_ui.detector_workspace.set_runtime', lambda: type('Runtime', (), {
        'state': lambda *args: None, 'result': lambda *args: result})())
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.detectors import render
render(context())
''', default_timeout=20).run()
    assert not app.exception
    assert any('30 из 60' in item.value and 'прогрев' in item.value for item in app.info)
    charts = [json.loads(item.proto.spec) for item in app.get('plotly_chart')]
    assert len(charts) == 1
    chart = charts[0]
    assert {trace['xaxis'] for trace in chart['data']} == {'x'}
    assert {trace['yaxis'] for trace in chart['data']} == {'y', 'y2'}
    assert chart['layout']['hoversubplots'] == 'axis'
    assert chart['layout']['xaxis']['showspikes']
    assert chart['layout']['xaxis']['spikemode'] == 'across'
    assert 'xaxis2' not in chart['layout']
    assert chart['layout']['legend']['orientation'] == 'h'
    assert chart['layout']['legend']['y'] < chart['layout']['yaxis']['domain'][0]
    assert chart['layout']['legend2']['y'] < 0
    assert chart['data'][0]['line']['color'] == '#2563eb'
    reactions = [trace for trace in chart['data'] if trace['yaxis'] == 'y2'
                 and trace.get('meta', {}).get('role') != 'cursor-guide']
    assert {trace['line']['color'] for trace in reactions} == {'#dc2626', '#16a34a'}
    assert all('detector_instance' not in trace['name'] for trace in reactions)
    trend = next(trace for trace in reactions if 'Trend' in trace['name'])
    assert all(value is None for value in trend['y'])
    guide = next(trace for trace in chart['data'] if trace.get('meta', {}).get('role') == 'cursor-guide')
    assert guide['opacity'] == 0 and guide['hoverinfo'] == 'none' and not guide['showlegend']


@pytest.mark.parametrize(('points', 'window'), [(60, 20), (240, 40)])
def test_single_ready_zero_is_visible_and_warmup_is_not_filled_with_zeros(monkeypatch, points, window):
    store, uid = setup_store()
    item = attach_detectors(store.metric_sets()[0], store.catalog_metrics(), [uid], ['Trend'], [])
    binding = next(iter(item['members'][0]['detectors'].values()))
    binding['parameters'] = {'window': window, 'data_max_size': points}
    store.save_metric_set(item, expected_revision=store.revision)

    def reader(source, first, last, metrics, *, step):
        return {m['id']: {**m, 'series': [{'labels': {},
            'times': [BASE + i * 15_000_000_000 for i in range(points)],
            'values': [15.] * points}]} for m in metrics}, 0

    runtime = SetRuntime(reader=reader)
    try:
        runtime.start(store, 'set', BASE, BASE + points * 15_000_000_000, 15)
        finish(runtime)
        result = runtime.result(store, 'set')
    finally:
        runtime.shutdown()
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    monkeypatch.setattr('atropos_ui.detector_workspace.set_runtime', lambda: type('Runtime', (), {
        'state': lambda *args: None, 'result': lambda *args: result})())
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.detectors import render
render(context())
''', default_timeout=20).run()
    assert not app.exception
    assert any(f'готовых оценок 1 из {points}' in item.value for item in app.info)
    charts = [json.loads(c.proto.spec) for c in app.get('plotly_chart')]
    assert len(charts) == 1
    trace = next(trace for trace in charts[0]['data'] if trace['yaxis'] == 'y2')
    assert trace['y'] == [None] * (points - 1) + [0.]
    assert trace['mode'] == 'lines+markers'
    assert trace['marker']['size'][-1] == 8


def test_new_trend_bindings_use_fifteen_minute_history_at_fifteen_second_step():
    store, uid = setup_store()
    item = attach_detectors(store.metric_sets()[0], store.catalog_metrics(), [uid], ['Trend'], [])
    binding = next(iter(item['members'][0]['detectors'].values()))
    assert binding['parameters'] == {'window': 20, 'data_max_size': 60}


def test_source_without_reactions_remains_a_single_panel():
    from atropos_ui.result_charts import result_figures

    entities = [{'kind': 'metric', 'name': 'cpu', 'labels': {},
                 'times': [BASE, BASE + 15_000_000_000], 'values': [1., 2.]}]
    figures = result_figures(entities)
    assert len(figures) == 1 and figures[0][0] == 'metric'
    assert list(figures[0][2].data[0].y) == [1., 2.]
    assert result_figures([]) == []
