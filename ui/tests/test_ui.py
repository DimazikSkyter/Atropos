from pathlib import Path

from streamlit.testing.v1 import AppTest

from atropos_ui.auth import password_hash

APP = Path(__file__).resolve().parents[1] / "app.py"


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception


def test_auth_gate_without_configuration(monkeypatch, ui_store):
    monkeypatch.delenv("ATROPOS_UI_DEMO", raising=False)
    monkeypatch.delenv("ATROPOS_UI_PASSWORD_HASH", raising=False)
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert app.title[0].value == "Вход в Atropos"
    assert not app.text_area


def test_demo_query_save_and_navigation_and_logout(monkeypatch, ui_store):
    monkeypatch.setenv("ATROPOS_UI_DEMO", "1")
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    click(app, "Войти в деморежим")
    assert not any(item.label == "Сохранённые определения" for item in app.selectbox)
    click(app, "Построить временной ряд")
    next(item for item in app.text_input if item.label == "Имя метрики").set_value("cpu_saved")
    click(app, "Добавить в каталог")
    assert ui_store.definitions()[0].name == "cpu_saved"
    click(app, "Открыть в каталоге")
    assert app.title[0].value == "Каталог метрик и конструктор селектора"
    click(app, "Открыть в запросе")
    assert app.title[0].value == "PromQL · рабочая область"
    click(app, "Выйти")
    assert app.title[0].value == "Вход в Atropos"
    assert "query_result" not in app.session_state


def test_server_page_and_datasource_page(monkeypatch, ui_store):
    monkeypatch.setenv("ATROPOS_UI_DEMO", "1")
    # Page functions tested independently; navigation itself is exercised above.
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.server import render
render(context())
''').run()
    click(app, "Получить фактический /metrics")
    assert "# TYPE atropos_demo_value gauge" in app.code[0].value
    click(app, "История для локального отображения")
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.datasources import render
render(context())
''').run()
    next(x for x in app.text_input if x.label == "Название").set_value("Test VM")
    click(app, "Сохранить")
    assert len(app.dataframe[0].value) == 2


def test_password_login_and_save_metric(monkeypatch, ui_store):
    from atropos_ui.demo import DemoClient
    monkeypatch.delenv("ATROPOS_UI_DEMO", raising=False)
    monkeypatch.setenv("ATROPOS_UI_PASSWORD_HASH", password_hash("test-password-long"))
    monkeypatch.setenv("ATROPOS_UI_USERNAME", "operator")
    monkeypatch.setattr("atropos_ui.context.Context.source_client", lambda self, source: DemoClient())
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.text_input[0].set_value("operator")
    app.text_input[1].set_value("wrong")
    click(app, "Войти")
    assert app.error and "identity" not in app.session_state
    app.session_state["login_retry_after"] = 0
    app.text_input[1].set_value("test-password-long")
    click(app, "Войти")
    assert app.session_state["identity"] == "operator"
    click(app, "Текущее значение")
    click(app, "Добавить в каталог")
    assert len(ui_store.definitions()) == 1


def test_load_saved_metric_into_editor(monkeypatch, ui_store):
    monkeypatch.setenv("ATROPOS_UI_DEMO", "1")
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    click(app, "Войти в деморежим")
    app.text_area[0].set_value("sum(up)")
    click(app, "Текущее значение")
    click(app, "Добавить в каталог")
    click(app, "Открыть в каталоге")
    click(app, "Открыть в запросе")
    assert app.text_area[0].value == "sum(up)"
    assert not any(button.label == "Добавить в каталог" for button in app.button)
    click(app, "Текущее значение")
    assert any(button.label == "Обновить в каталоге" for button in app.button)
