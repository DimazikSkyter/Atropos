from datetime import UTC, datetime
from uuid import uuid4

import streamlit as st

from atropos_ui.service_api import DATASOURCE_KINDS, DEFAULT_URLS, Datasource

AUTH_LABELS = {"none": "Без авторизации", "basic": "Basic auth", "bearer": "Bearer token", "token": "API token"}
INFLUX_LABELS = {"1": "1.x · InfluxQL", "2": "2.x · Flux", "3": "3.x Core · HTTP SQL"}
NEW_SOURCE = "+"


def show_health(result):
    {"ok": st.success, "error": st.error, "demo": st.info}[result.status](result.message)
    st.caption("Проверено: " + datetime.fromtimestamp(result.checked_at, UTC).strftime("%Y-%m-%d %H:%M:%S UTC"))


def render(ctx):
    st.title("Источники данных")
    st.caption("Подключения, настройки доступа и проверка соединения с хранилищами.")
    service = ctx.datasources
    sources = service.list()
    by_uid = {source.id: source for source in sources}
    pending = st.session_state.pop("datasource_pending", False)
    if pending is not False:
        st.session_state["datasource_selection"] = pending or NEW_SOURCE
    if st.session_state.get("datasource_selection") not in by_uid:
        st.session_state["datasource_selection"] = NEW_SOURCE
    feedback = st.session_state.pop("datasource_feedback", None)
    if feedback:
        st.success(feedback[0])
        if feedback[1]:
            show_health(feedback[1])
    choice = st.selectbox(
        "Подключение", [NEW_SOURCE, *by_uid], key="datasource_selection",
        format_func=lambda uid: "＋ Новое подключение" if uid == NEW_SOURCE else
        by_uid[uid].name + (" · по умолчанию" if by_uid[uid].is_default else ""),
    )
    uid = None if choice == NEW_SOURCE else choice
    selected = by_uid.get(uid)
    prefix = "datasource_" + (uid or "new")
    revision_key = prefix + "_revision"
    if selected:
        st.session_state.setdefault(revision_key, selected.revision)
    kind = st.selectbox("Тип", DATASOURCE_KINDS, key=prefix + "_kind",
                        index=DATASOURCE_KINDS.index(selected.kind) if selected else 0)
    version = "2"
    if kind == "InfluxDB":
        version = st.selectbox("Версия InfluxDB", ["1", "2", "3"], format_func=INFLUX_LABELS.get,
                               index=["1", "2", "3"].index(selected.influx_version) if selected and selected.kind == kind else 1,
                               key=prefix + "_version")
    active = selected if selected and selected.kind == kind and (kind != "InfluxDB" or selected.influx_version == version) else None
    choices = ["none", "basic", "bearer"] if kind in {"VictoriaMetrics", "Prometheus"} else ["none", "basic"]
    if kind == "InfluxDB" and version != "1":
        choices = ["token", "none"]
    auth = st.selectbox("Авторизация", choices, format_func=AUTH_LABELS.get,
                        index=choices.index(active.auth_type) if active and active.auth_type in choices else 0,
                        key=f"{prefix}_{kind}_{version}_auth")
    form_key = f"{prefix}_{kind}_{version}_{auth}"
    default_url = "http://localhost:8181" if kind == "InfluxDB" and version == "3" else DEFAULT_URLS[kind]
    with st.form(form_key):
        st.subheader("Подключение")
        name = st.text_input("Название", selected.name if selected else "", key=form_key + "_name")
        url = st.text_input("Базовый URL", active.url if active else default_url, key=form_key + "_url")
        if kind == "VictoriaMetrics":
            st.caption("VM Cluster: http://vmselect:8481/select/0/prometheus — без /api/v1.")
        if kind == "ClickHouse":
            st.caption("Используется HTTP-интерфейс ClickHouse, обычно порт 8123 или HTTPS 8443.")
        is_default = st.checkbox("Источник по умолчанию", selected.is_default if selected else not sources)
        database = organization = bucket = ""
        if kind == "ClickHouse" or (kind == "InfluxDB" and version in {"1", "3"}):
            database = st.text_input("База данных", active.database if active else ("default" if kind == "ClickHouse" else ""))
        if kind == "InfluxDB" and version == "2":
            organization = st.text_input("Организация", active.organization if active else "")
            bucket = st.text_input("Bucket", active.bucket if active else "")
        token_env = username = password_env = ""
        if auth in {"bearer", "token"}:
            token_env = st.text_input("Переменная окружения с токеном", active.token_env if active else "")
            st.caption("Укажите имя переменной, например INFLUX_TOKEN. Сам токен не сохраняется в настройках.")
        elif auth == "basic":
            username = st.text_input("Пользователь", active.username if active else "")
            password_env = st.text_input("Переменная окружения с паролем", active.password_env if active else "")
        st.subheader("Параметры запросов")
        a, b = st.columns(2)
        timeout = a.number_input("Timeout, секунд", 1, 300, int(active.timeout_seconds) if active else 20)
        interval = b.number_input("Минимальный шаг, секунд", 1, 86400, active.min_interval_seconds if active else 15)
        tls_verify = st.checkbox("Проверять TLS-сертификат", active.tls_verify if active else True)
        save = st.form_submit_button("Сохранить")
        save_test = st.form_submit_button("Сохранить и проверить", type="primary")
    if save or save_test:
        try:
            source = Datasource(uid or uuid4().hex, name, url, kind, token_env,
                                is_default=is_default, auth_type=auth, username=username, password_env=password_env,
                                database=database, organization=organization, bucket=bucket, influx_version=version,
                                timeout_seconds=timeout, tls_verify=tls_verify, min_interval_seconds=interval,
                                revision=st.session_state[revision_key] if selected else 1)
            saved = service.save(source)
            st.session_state["datasource_" + saved.id + "_revision"] = saved.revision
            checks = st.session_state.setdefault("datasource_checks", {})
            checks.pop(saved.id, None)
            result = service.test_connection(saved.id) if save_test else None
            if result:
                checks[saved.id] = (saved, result)
            st.session_state["datasource_feedback"] = ("Подключение сохранено.", result)
            st.session_state["datasource_pending"] = saved.id
            st.rerun()
        except (ValueError, KeyError) as exc:
            st.error(str(exc))
    if selected:
        st.caption(f"UID: {selected.id} · Ревизия: {selected.revision} · Язык запросов: {selected.query_language}")
        if not selected.is_default and st.button("Сделать источником по умолчанию"):
            changed = service.set_default(selected.id)
            st.session_state[revision_key] = changed.revision
            st.rerun()
        if st.button("Проверить сохранённое подключение"):
            with st.spinner("Проверяем доступ…"):
                result = service.test_connection(selected.id)
            st.session_state.setdefault("datasource_checks", {})[selected.id] = (selected, result)
        checked = st.session_state.get("datasource_checks", {}).get(selected.id)
        if checked and checked[0] == selected and not (feedback and feedback[1]):
            show_health(checked[1])
        confirm = st.checkbox("Подтверждаю удаление подключения", key=prefix + "_confirm")
        if st.button("Удалить подключение", disabled=not confirm):
            try:
                service.delete(selected.id)
                st.session_state.setdefault("datasource_checks", {}).pop(selected.id, None)
                st.session_state["datasource_pending"] = None
                st.rerun()
            except (ValueError, KeyError) as exc:
                st.error(str(exc))
    st.divider()
    st.subheader("Подключения")
    search = st.text_input("Поиск источников", placeholder="Название подключения")
    rows = []
    for source in service.list(search=search):
        checked = st.session_state.get("datasource_checks", {}).get(source.id)
        status = {"ok": "Доступен", "error": "Ошибка", "demo": "Демо"}[checked[1].status] if checked and checked[0] == source else "Не проверен"
        rows.append({"Название": source.name, "Тип": source.kind, "По умолчанию": source.is_default,
                     "URL": source.url, "Язык": source.query_language, "Проверка": status, "UID": source.id})
    st.dataframe(rows, hide_index=True, use_container_width=True)
