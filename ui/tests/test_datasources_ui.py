import pytest
from streamlit.testing.v1 import AppTest

from atropos_ui.models import Datasource, MetricDefinition
from atropos_ui.service_api import DatasourceHealth, DatasourceService

PAGE = '''
from atropos_ui.context import context
from atropos_ui.pages.datasources import render
render(context())
'''


def widget(elements, label):
    return next(element for element in elements if element.label == label)


def click(app, label):
    widget(app.button, label).click().run()
    assert not app.exception


@pytest.mark.parametrize('kind', ['VictoriaMetrics', 'Prometheus', 'InfluxDB', 'ClickHouse'])
def test_create_test_edit_and_delete_each_type_in_ui(monkeypatch, ui_store, kind):
    monkeypatch.setenv('ATROPOS_UI_DEMO', '1')
    app = AppTest.from_string(PAGE, default_timeout=15).run()
    widget(app.selectbox, 'Тип').set_value(kind).run()
    widget(app.text_input, 'Название').set_value('Test ' + kind)
    if kind == 'InfluxDB':
        widget(app.text_input, 'Организация').set_value('test-org')
        widget(app.text_input, 'Bucket').set_value('metrics')
        widget(app.text_input, 'Переменная окружения с токеном').set_value('INFLUX_TOKEN')
    widget(app.checkbox, 'Источник по умолчанию').check()
    click(app, 'Сохранить и проверить')
    store = ui_store
    service = DatasourceService(store)
    source = next(source for source in service.list() if source.name == 'Test ' + kind)
    assert source.kind == kind and service.default().id == source.id
    assert any('не проверялось' in info.value for info in app.info)
    assert len(app.dataframe[0].value) == 2
    assert app.dataframe[0].value.loc[app.dataframe[0].value['UID'] == source.id, 'Проверка'].iloc[0] == 'Демо'
    widget(app.text_input, 'Название').set_value('Renamed ' + kind)
    click(app, 'Сохранить')
    assert service.get(source.id).name == 'Renamed ' + kind
    assert service.get(source.id).id == source.id
    assert app.dataframe[0].value.loc[app.dataframe[0].value['UID'] == source.id, 'Проверка'].iloc[0] == 'Не проверен'
    widget(app.checkbox, 'Подтверждаю удаление подключения').check().run()
    click(app, 'Удалить подключение')
    assert len(service.list()) == 1
    assert service.default().id == 'local-vm'


@pytest.mark.parametrize('version', ['1', '3'])
def test_influx_version_specific_fields(monkeypatch, ui_store, version):
    monkeypatch.setenv('ATROPOS_UI_DEMO', '1')
    app = AppTest.from_string(PAGE, default_timeout=15).run()
    widget(app.selectbox, 'Тип').set_value('InfluxDB').run()
    widget(app.selectbox, 'Версия InfluxDB').set_value(version).run()
    widget(app.text_input, 'Название').set_value('Influx ' + version)
    widget(app.text_input, 'База данных').set_value('metrics')
    if version == '3':
        widget(app.text_input, 'Переменная окружения с токеном').set_value('INFLUX_TOKEN')
    assert not any(element.label == 'Организация' for element in app.text_input)
    click(app, 'Сохранить')
    source = next(source for source in ui_store.sources() if source.kind == 'InfluxDB')
    assert source.influx_version == version and source.database == 'metrics'


def test_ui_connection_failure_preserves_saved_configuration(monkeypatch, ui_store):
    monkeypatch.delenv('ATROPOS_UI_DEMO', raising=False)
    monkeypatch.setattr('atropos_ui.context.check_datasource',
                        lambda source: DatasourceHealth(source.id, 'error', 'Проверка соединения завершилась ошибкой.', 1))
    app = AppTest.from_string(PAGE, default_timeout=15).run()
    widget(app.text_input, 'Название').set_value('Unavailable VM')
    click(app, 'Сохранить и проверить')
    assert app.error
    assert any('сохранено' in success.value for success in app.success)
    source = next(source for source in ui_store.sources() if source.name == 'Unavailable VM')
    assert app.dataframe[0].value.loc[app.dataframe[0].value['UID'] == source.id, 'Проверка'].iloc[0] == 'Ошибка'


def test_used_source_deletion_is_blocked_in_ui(monkeypatch, ui_store):
    monkeypatch.setenv('ATROPOS_UI_DEMO', '1')
    app = AppTest.from_string(PAGE, default_timeout=15).run()
    ui_store.save(MetricDefinition('derived', 'local-vm', 'up'))
    widget(app.selectbox, 'Подключение').select_index(1).run()
    widget(app.checkbox, 'Подтверждаю удаление подключения').check().run()
    click(app, 'Удалить подключение')
    assert any('используется' in error.value for error in app.error)
    assert len(ui_store.sources()) == 1


def test_promql_uses_compatible_sources_and_their_minimum_step(monkeypatch, ui_store):
    monkeypatch.setenv('ATROPOS_UI_DEMO', '1')
    store = ui_store
    store.delete("source", "local-vm")
    store.save(Datasource('vm', 'VM', 'http://vm', min_interval_seconds=60))
    store.save(Datasource('ch', 'Warehouse', 'http://ch', 'ClickHouse', is_default=True))
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.queries import render
render(context())
''', default_timeout=15).run()
    assert not app.exception
    assert widget(app.selectbox, 'Источник').value.id == 'vm'
    assert widget(app.number_input, 'Шаг, секунд').value == 60
    assert 'Warehouse' not in widget(app.selectbox, 'Источник').options
