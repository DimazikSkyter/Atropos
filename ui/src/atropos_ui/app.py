import os
from functools import partial

import streamlit as st

from atropos_ui.auth import login_page
from atropos_ui.clients import ApiError
from atropos_ui.context import context
from atropos_ui.pages import (
    account,
    catalog,
    datasources,
    detectors,
    live,
    monitoring,
    queries,
    server,
)
from atropos_ui.service_api import AccountStore
from atropos_ui.storage import ConfigurationError


def main():
    st.set_page_config(page_title="Atropos · Metrics", page_icon="📈", layout="wide")
    if os.getenv('ATROPOS_UI_DEMO') != '1':
        try:
            from atropos_ui.replay_ui import runtime
            runtime()
        except ConfigurationError:
            # Login remains available and explains a missing/migrating database.
            pass
    if "identity" not in st.session_state:
        st.navigation([st.Page(login_page, title="Авторизация", url_path="login")]).run()
        return
    try:
        ctx = context()
    except (OSError, ConfigurationError):
        st.error("Не удалось открыть конфигурацию PostgreSQL. Проверьте ATROPOS_CONFIG_DATABASE_URL и выполните make service-up.")
        return
    with st.sidebar:
        st.title("Atropos")
        st.caption("METRICS WORKSPACE")
        st.write("Пользователь: " + st.session_state["identity"])
        if st.button("Выйти", use_container_width=True):
            st.session_state.clear()
            st.rerun()
    if ctx.demo:
        st.warning("ДЕМО · Синтетические данные. PromQL не вычисляется, сетевые запросы отключены.")
    elif isinstance(ctx.store, AccountStore):
        from atropos_ui.replay_ui import on_login
        on_login(ctx.store)
    pages = [
        st.Page(partial(queries.render, ctx), title="PromQL", icon="🔎", url_path="queries", default=True),
        st.Page(partial(catalog.render, ctx), title="Каталог метрик", icon="📚", url_path="catalog"),
        st.Page(partial(datasources.render, ctx), title="Источники данных", icon="🔌", url_path="datasources"),
        st.Page(partial(server.render, ctx), title="Серверные метрики", icon="📈", url_path="server"),
        st.Page(partial(detectors.render, ctx), title="Детекторы", icon="🧪", url_path="detectors"),
        st.Page(partial(monitoring.render, ctx), title="Мониторинг реакций", icon="📊", url_path="monitoring"),
        st.Page(partial(live.render, ctx), title="Рантайм реакций", icon="▶️", url_path="runtime"),
        st.Page(partial(account.render, ctx), title="Настройки учётки", icon="⚙️", url_path="account"),
    ]
    try:
        navigation = st.navigation(pages)
        requested = st.session_state.pop('requested_page', None)
        if requested in {'queries', 'catalog'}:
            st.switch_page(pages[0 if requested == 'queries' else 1])
        navigation.run()
    except (OSError, ConfigurationError):
        st.error("Не удалось прочитать или сохранить конфигурацию PostgreSQL. Проверьте ATROPOS_CONFIG_DATABASE_URL и выполните make service-up.")
    except ValueError as exc:
        st.error(str(exc))
    except ApiError as exc:
        st.error('Сервер вычислений недоступен: ' + str(exc))
