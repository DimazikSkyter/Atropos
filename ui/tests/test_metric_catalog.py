import json
from copy import deepcopy
from dataclasses import asdict

import pytest
from streamlit.testing.v1 import AppTest

from atropos_ui.clients import METRIC_SEARCH_LIMIT
from atropos_ui.demo import DemoClient
from atropos_ui.models import MetricDefinition
from atropos_ui.service_api import Datasource, MemoryConfigRepository

QUERY_PAGE = '''
from atropos_ui.context import context
from atropos_ui.pages.queries import render
render(context())
'''
CATALOG_PAGE = '''
from atropos_ui.context import context
from atropos_ui.pages.catalog import render
render(context())
'''


def widget(elements, label):
    return next(item for item in elements if item.label == label)


def click(app, label):
    widget(app.button, label).click().run()
    assert not app.exception


def test_transform_preserves_samples_and_input_and_changes_only_labels():
    data = {'resultType': 'matrix', 'result': [
        {'metric': {'__name__': 'raw', 'instance': 'a', 'job': 'old'}, 'values': [[1, 'NaN'], [2, '0']]},
        {'metric': {'__name__': 'raw', 'instance': 'b', 'job': 'old'}, 'values': [[1, '+Inf'], [2, '2']]},
    ]}
    before = deepcopy(data)
    item = MetricDefinition('renamed', 'vm', 'sum(raw)', drop_labels=('job',), extra_labels={'team': 'наблюдение', 'job': 'new'})
    result = item.transform_result(data)
    assert data == before
    assert result['result'][0]['metric'] == {'__name__': 'renamed', 'instance': 'a', 'team': 'наблюдение', 'job': 'new'}
    assert [row['values'] for row in result['result']] == [row['values'] for row in data['result']]
    assert not {'start', 'end', 'step', 'window', 'samples', 'values'} & asdict(item).keys()


def test_transform_refuses_series_collision_instead_of_merging():
    item = MetricDefinition('cpu', 'vm', 'up', drop_labels=('instance',))
    data = {'resultType': 'vector', 'result': [
        {'metric': {'instance': 'a'}, 'value': [1, '1']},
        {'metric': {'instance': 'b'}, 'value': [1, '2']},
    ]}
    with pytest.raises(ValueError, match='одинаковыми'):
        item.transform_result(data)


@pytest.mark.parametrize('settings', [
    {'drop_labels': ('__name__',)}, {'drop_labels': 'instance'},
    {'extra_labels': {'bad-key': 'value'}}, {'extra_labels': {'__name__': 'value'}},
    {'extra_labels': {'team': 12}},
])
def test_invalid_label_operations_are_rejected(settings):
    with pytest.raises(ValueError):
        MetricDefinition('cpu', 'vm', 'up', **settings)


def test_memory_catalog_rename_and_duplicate_protection():
    store = MemoryConfigRepository()
    store.save(Datasource('vm', 'VM', 'http://vm'))
    old = MetricDefinition('old', 'vm', 'up')
    store.save_definition(old)
    with pytest.raises(ValueError, match='уже есть'):
        store.save_definition(old)
    new = MetricDefinition('new', 'vm', 'rate(raw[5m])', extra_labels={'team': 'ops'})
    store.save_definition(new, previous_name='old')
    assert store.definitions() == [new]


def test_query_name_tags_save_and_catalog_preview(monkeypatch, ui_store):
    monkeypatch.setenv('ATROPOS_UI_DEMO', '1')
    app = AppTest.from_string(QUERY_PAGE, default_timeout=15).run()
    assert not any(item.label == 'Сохранённые определения' for item in app.selectbox)
    assert not any(item.value == 'Определение новой метрики' for item in app.subheader)
    assert not app.expander
    app.text_area[0].set_value('sum(up)')
    click(app, 'Построить временной ряд')
    widget(app.text_input, 'Имя метрики').set_value('cpu_saved')
    widget(app.text_input, 'Новый тег').set_value('team')
    widget(app.text_input, 'Значение тега').set_value('ops')
    click(app, 'Добавить тег')
    click(app, 'Добавить в каталог')
    saved = ui_store.definitions()[0]
    assert saved.name == 'cpu_saved' and saved.query == 'sum(up)'
    assert saved.extra_labels == {'team': 'ops'}
    assert saved.drop_labels == ()
    assert any('Период' in item.value for item in app.success)
    click(app, 'Открыть в каталоге')
    assert app.session_state['requested_page'] == 'catalog'
    catalog = AppTest.from_string(CATALOG_PAGE, default_timeout=15).run()
    widget(catalog.radio, 'Раздел каталога').set_value('Сохранённые метрики').run()
    click(catalog, 'Построить временной ряд')
    labels = [json.loads(value) for value in catalog.dataframe[-1].value.series.unique()]
    assert labels and all(row['__name__'] == 'cpu_saved' and row['team'] == 'ops' for row in labels)
    click(catalog, 'Открыть в запросе')
    assert catalog.session_state['pending_query']['query'] == 'sum(up)'
    assert catalog.session_state['pending_query']['extra_labels'] == {'team': 'ops'}
    assert not {'window', 'start', 'end', 'step'} & catalog.session_state['pending_query'].keys()


def test_dropping_identity_tag_blocks_catalog_save(monkeypatch, ui_store):
    monkeypatch.setenv('ATROPOS_UI_DEMO', '1')
    app = AppTest.from_string(QUERY_PAGE, default_timeout=15).run()
    click(app, 'Построить временной ряд')
    widget(app.multiselect, 'Удалить теги').set_value(['instance']).run()
    assert any('одинаковыми' in error.value for error in app.error)
    assert widget(app.button, 'Добавить в каталог').disabled
    assert not ui_store.definitions()


def test_source_catalog_selector_opens_query(monkeypatch, ui_store):
    monkeypatch.setenv('ATROPOS_UI_DEMO', '1')
    app = AppTest.from_string(CATALOG_PAGE, default_timeout=15).run()
    widget(app.text_input, 'Поиск метрик').set_value('http').run()
    click(app, 'Загрузить теги и ряды')
    widget(app.selectbox, 'Конкретный ряд').set_value(app.session_state['catalog_series'][1][0]).run()
    click(app, 'Открыть в запросе')
    assert 'instance=' in app.session_state['pending_query']['query']
    assert '__name__' in app.session_state['pending_query']['query']
    assert app.session_state['requested_page'] == 'queries'


def test_source_catalog_waits_for_three_characters_and_reuses_search(monkeypatch, ui_store):
    monkeypatch.setenv('ATROPOS_UI_DEMO', '1')
    searches = []

    def names(self, search):
        searches.append(search)
        return [search + '_total']

    monkeypatch.setattr(DemoClient, 'names', names)
    app = AppTest.from_string(CATALOG_PAGE, default_timeout=15).run()
    assert not searches
    assert widget(app.button, 'Обновить результаты').disabled
    for text in ('a', 'ab', '  ab  ', '   '):
        widget(app.text_input, 'Поиск метрик').set_value(text).run()
        assert not app.exception
        assert not searches
        assert not any(item.label == 'Метрика' for item in app.selectbox)

    widget(app.text_input, 'Поиск метрик').set_value('  cpu  ').run()
    assert searches == ['cpu']
    assert widget(app.selectbox, 'Метрика').value == 'cpu_total'
    click(app, 'Загрузить теги и ряды')
    click(app, 'Текущее значение')
    assert searches == ['cpu']
    assert 'catalog_series' in app.session_state
    assert 'catalog_preview' in app.session_state

    widget(app.text_input, 'Поиск метрик').set_value('mem').run()
    assert searches == ['cpu', 'mem']
    assert 'catalog_series' not in app.session_state
    assert 'catalog_preview' not in app.session_state
    click(app, 'Обновить результаты')
    assert searches == ['cpu', 'mem', 'mem']

    ui_store.save(Datasource('other', 'Other VM', 'http://other-vm'))
    app.run()
    widget(app.selectbox, 'Источник').select('Other VM').run()
    assert not app.exception
    assert searches == ['cpu', 'mem', 'mem', 'mem']

    widget(app.text_input, 'Поиск метрик').set_value('me').run()
    assert len(searches) == 4
    assert 'catalog_names' not in app.session_state
    assert widget(app.button, 'Обновить результаты').disabled


def test_source_catalog_bounds_choices_and_handles_empty_search(monkeypatch, ui_store):
    monkeypatch.setenv('ATROPOS_UI_DEMO', '1')
    monkeypatch.setattr(DemoClient, 'names', lambda self, search:
                        [f'http_{i}' for i in range(METRIC_SEARCH_LIMIT + 1)] if search == 'http' else [])
    app = AppTest.from_string(CATALOG_PAGE, default_timeout=15).run()
    widget(app.text_input, 'Поиск метрик').set_value('http').run()
    assert not app.exception
    assert len(widget(app.selectbox, 'Метрика').options) == METRIC_SEARCH_LIMIT
    assert any('Уточните поиск' in item.value for item in app.warning)
    widget(app.text_input, 'Поиск метрик').set_value('absent').run()
    assert not app.exception
    assert not any(item.label == 'Метрика' for item in app.selectbox)
    assert any('Метрики не найдены' in item.value for item in app.info)
