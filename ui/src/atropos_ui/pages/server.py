import streamlit as st
from prometheus_client.parser import text_string_to_metric_families

from atropos_ui.clients import ApiError
from atropos_ui.components import render_result, time_window
from atropos_ui.service_api import ServiceSettings
from atropos_ui.time_ranges import format_bounds


def render(ctx):
    st.title("Серверные метрики")
    st.caption("Новые метрики: текущее значение, история вычислений и фактический ответ /metrics.")
    if not ctx.demo:
        settings = ctx.store.settings()
        if st.session_state.get("server_scope") != settings:
            for key in ("exposition", "server_result"):
                st.session_state.pop(key, None)
            st.session_state["server_scope"] = settings
        with st.expander("Настройки вычислительного сервера"):
            with st.form("server_settings"):
                url = st.text_input("URL сервера", settings.server_url)
                token_env = st.text_input("Переменная окружения с токеном сервера", settings.server_token_env)
                if st.form_submit_button("Сохранить настройки сервера"):
                    try:
                        ctx.store.save_settings(ServiceSettings(url, token_env))
                        for key in ("exposition", "server_result"):
                            st.session_state.pop(key, None)
                        st.success("Настройки сервера сохранены в PostgreSQL.")
                    except ValueError as exc:
                        st.error(str(exc))
    client = ctx.server_client()
    if not ctx.demo:
        st.link_button("Открыть /metrics на сервере ↗", client.url + "/metrics")
        st.caption("Ссылка открывается в браузере. Bearer token UI в неё не включается.")
        st.code(client.url, language=None)
    else:
        st.info("Демонстрация /metrics: ниже синтетический ответ. Реального сервера нет.")
    if st.button("Загрузить определения с сервера"):
        try:
            st.dataframe(client.definitions(), use_container_width=True)
        except ApiError as exc:
            st.error(str(exc))
    st.subheader("Проверка /metrics")
    if st.button("Получить фактический /metrics", type="primary"):
        st.session_state.pop("exposition", None)
        try:
            st.session_state["exposition"] = client.exposition()
        except ApiError as exc:
            st.error(str(exc))
    raw = st.session_state.get("exposition")
    if raw is not None:
        st.code(raw, language="text")
        st.download_button("Скачать ответ", raw, file_name="metrics.txt", mime="text/plain")
        try:
            samples = [{"metric": sample.name, "labels": str(sample.labels), "value": sample.value}
                       for family in text_string_to_metric_families(raw) for sample in family.samples]
            st.dataframe(samples, use_container_width=True, hide_index=True)
            if not samples:
                st.info("В ответе нет samples.")
        except ValueError:
            st.error("Ответ не соответствует поддерживаемому Prometheus text format. Исходный текст показан выше.")
    st.divider()
    st.subheader("Значения вычисленных метрик")
    query = st.text_input("PromQL к серверным метрикам", "atropos_derived_value")
    selection, step = time_window("server")
    a, b = st.columns(2)
    instant = a.button("Текущее значение сервера")
    ranged = b.button("История для локального отображения")
    if instant or ranged:
        st.session_state.pop("server_result", None)
        try:
            bounds = selection.resolve() if ranged else None
            data = client.range(query, *bounds, step) if ranged else client.instant(query)
            st.session_state["server_result"] = {"query": query, "data": data,
                                                  "window": (selection, step) if ranged else None, "bounds": bounds}
        except (ApiError, ValueError) as exc:
            st.error(str(exc))
    result = st.session_state.get("server_result")
    if result:
        st.caption("Результат последнего запроса: " + result["query"])
        if result["query"] != query or (result["window"] and result["window"] != (selection, step)):
            st.warning("Параметры изменились; выполните запрос повторно.")
        if result.get('bounds'):
            st.caption('Данные за ' + format_bounds(result['bounds'], result['window'][0].timezone)
                       + ' · ' + result['window'][0].timezone)
        try:
            render_result(result["data"])
        except ApiError as exc:
            st.error(str(exc))
