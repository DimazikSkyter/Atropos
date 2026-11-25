"""Runtime set configuration and selective, opt-in visualization."""
import json
from uuid import uuid4

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from atropos_ui.detector_runtime import DETECTORS
from atropos_ui.live_runtime import display_time, window_bounds
from atropos_ui.live_ui import live_runtime
from atropos_ui.run_info import metadata_panel
from atropos_ui.service_api import (
    AccountStore,
    catalog_metrics,
    detector_instances,
    validate_metric_set,
)


def set_editor(store, workspace, revision):
    metrics = {m['id']: m for m in catalog_metrics(workspace)}
    sets = {s['id']: s for s in workspace.get('metric_sets', [])}
    selected = st.selectbox('Редактировать сет', [None, *sets],
        format_func=lambda uid: 'Новый сет' if uid is None else sets[uid]['name'], key='live_edit_set')
    item = sets.get(selected, {})
    mode = st.radio('Состав сета', ['Конструктор', 'JSON'], index=1 if selected else 0, horizontal=True,
                    key='live_editor_mode_' + str(selected))
    with st.form('live_set_form_' + str(selected) + '_' + str(revision)):
        name = st.text_input('Имя сета', item.get('name', 'Новый сет'))
        left, middle, right = st.columns(3)
        start = left.text_input('Начало окна', item.get('start', 'now() - 5m'))
        end = middle.text_input('Конец окна', item.get('end', 'now()'))
        interval = right.number_input('Обновлять каждые, секунд', min_value=1.0,
                                      value=float(item.get('refresh_seconds', 15)), step=1.0)
        if mode == 'Конструктор':
            selected_metrics = st.multiselect('Метрики сета', list(metrics),
                default=[m['metric_id'] for m in item.get('members', [])],
                format_func=lambda uid: metrics[uid]['name'] + ' · ' + uid)
            detectors = st.multiselect('Детекторы сета', list(DETECTORS), default=['MAD', 'CUSUM'])
            parameters = st.text_area('Параметры детекторов сета (JSON)', '{}',
                help='Например {"MAD": {"window": 20}}. Неуказанные параметры берутся по умолчанию.')
            peers = st.multiselect('Связанные метрики сета для Correlation', list(metrics),
                                  format_func=lambda uid: metrics[uid]['name'] + ' · ' + uid)
            st.caption('Все выбранные детекторы применяются ко всем выбранным метрикам. '
                       'Для индивидуальных параметров каждой комбинации используйте JSON.')
        else:
            members_json = st.text_area('Комбинации метрика × детекторы (JSON)',
                json.dumps(item.get('members', []), ensure_ascii=False, indent=2), height=280,
                help='Список объектов {"metric_id": "ID", "detectors": {"MAD": {}}, "peers": []}. '
                     'Одна метрика может участвовать в нескольких сетах с разными параметрами.')
        if st.form_submit_button('Сохранить сет', type='primary'):
            try:
                if mode == 'Конструктор':
                    overrides = json.loads(parameters)
                    if not isinstance(overrides, dict) or set(overrides) - set(detectors):
                        raise ValueError('Параметры должны быть объектом JSON только для выбранных детекторов.')
                    bindings = {d: overrides.get(d, {}) for d in detectors}
                    members = [{'metric_id': uid, 'detectors': bindings, 'peers': [p for p in peers if p != uid]}
                               for uid in selected_metrics]
                else:
                    members = json.loads(members_json)
                saved = validate_metric_set({'id': selected or uuid4().hex, 'name': name, 'start': start,
                    'end': end, 'refresh_seconds': interval, 'members': members}, list(metrics.values()))
                window_bounds(saved, pd.Timestamp.now(tz='UTC').timestamp())
                for member in saved['members']:
                    detector_instances(member['detectors'])
                store.save_metric_set(saved, expected_revision=revision)
                # The old session is retained with the configuration it actually calculated.
                live_runtime().stop_set(store, saved['id'])
                st.rerun()
            except (ValueError, TypeError, OverflowError) as exc:
                st.error(str(exc))
    if selected and st.button('Удалить сет', key='live_delete_set'):
        store.remove_metric_set(selected, expected_revision=revision)
        live_runtime().stop_set(store, selected)
        st.rerun()
    if st.checkbox('Показать ID зарегистрированных метрик', key='live_metric_ids'):
        st.dataframe([{'ID': uid, 'Метрика': m['name'], 'Источник': m['datasource_id']} for uid, m in metrics.items()],
                     hide_index=True, use_container_width=True)


def history_chart(data):
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True,
                           subplot_titles=('Накопленные исходные ряды', 'Реакции детекторов'))
    for source in data['sources']:
        figure.add_trace(go.Scattergl(x=pd.to_datetime(source['times'], unit='ns', utc=True).tz_convert('Europe/Moscow'),
            y=source['values'], mode='lines', name=source['name'] + ' · ' + json.dumps(source['labels'], ensure_ascii=False)),
            row=1, col=1)
    for component in data['components']:
        figure.add_trace(go.Scattergl(x=pd.to_datetime(component['times'], unit='ns', utc=True).tz_convert('Europe/Moscow'),
            y=component['scores'], mode='lines', connectgaps=False,
            name=component['detector'] + ' · ' + ' ↔ '.join(s['name'] + ' ' + json.dumps(s['labels'], ensure_ascii=False)
                                                         for s in component['sources'])), row=2, col=1)
    figure.update_layout(height=700, hovermode='x unified', uirevision='live-history',
                         legend=dict(itemclick='toggle', itemdoubleclick='toggleothers'))
    figure.update_yaxes(title_text='Значение', row=1, col=1)
    figure.update_yaxes(title_text='Реакция', row=2, col=1)
    return figure


@st.fragment(run_every='3s')
def sessions_panel(store):
    instance = live_runtime()
    sessions = instance.summaries(store)
    if not sessions:
        st.info('Запустите сет. Данные будут накапливаться в памяти процесса приложения.')
        return
    st.dataframe([{'Сет': s['name'], 'Сессия': s['id'][:8], 'Запущен': s['running'], 'Считает': s['in_flight'],
        'Окон': s['runs'], 'Исходных рядов': s['source_series'], 'Реакций': s['components'],
        'Исходных точек': s['source_points'], 'Точек реакций во всех окнах': s['reaction_points']}
        for s in sessions], hide_index=True, use_container_width=True)
    by_id = {s['id']: s for s in sessions}
    uid = st.selectbox('Сессия рантайма', list(by_id), format_func=lambda key:
        by_id[key]['name'] + ' · ' + display_time(by_id[key]['created_at']) + ' · ' + key[:8])
    session = by_id[uid]
    if session['running']:
        if st.button('Остановить сессию', key='live_stop_' + uid):
            instance.stop(store, uid)
            st.rerun()
    elif not session['in_flight'] and st.button('Удалить сессию и историю', key='live_delete_' + uid):
        instance.delete(store, uid)
        st.rerun()
    if session.get('evicted_runs'):
        st.caption(f"По лимиту хранения удалено окон: {session['evicted_runs']}. Номера относятся к сохранённым окнам.")
    history_tab, parameters = st.tabs(['История реакций', 'Параметры запуска'])
    with parameters:
        if session['runs']:
            number = st.selectbox('Параметры сохранённого окна', list(range(session['runs'])),
                index=session['runs'] - 1, format_func=lambda n: str(n + 1), key='live_metadata_' + uid)
            metadata_panel(instance.metadata(store, uid, number))
    st.caption(f"{session['start']} → {session['end']} · каждые {session['refresh_seconds']:g} с · {session['progress']}")
    if session['error']:
        st.error(session['error'])
    if session['last_run']:
        st.caption('Последнее окно рассчитано: ' + display_time(session['last_run']['finished_at']))
        for warning in session['last_run']['warnings']:
            st.warning(warning)
    with history_tab:
        history_panel(instance, store, uid, session)


def history_panel(instance, store, uid, session):
    if not st.checkbox('Показывать графики реакций', value=False, key='live_show_' + uid):
        st.caption('Фоновый расчёт всех комбинаций продолжается без построения графиков.')
        return
    options = {c['id']: c for c in instance.component_options(store, uid)}
    selected = st.multiselect('Реакции для отображения', list(options), max_selections=20,
        format_func=lambda key: options[key]['detector'] + ' · ' + ' ↔ '.join(
            s['name'] + ' ' + json.dumps(s['labels'], ensure_ascii=False) for s in options[key]['sources']),
        key='live_components_' + uid)
    if not selected:
        st.info('Выберите до 20 реакций. Все остальные продолжают рассчитываться в фоне.')
        return
    mode = st.radio('История реакций', ['Накопленная', 'Отдельное окно'], horizontal=True, key='live_history_' + uid)
    run = None
    if mode == 'Отдельное окно':
        if not session['runs']:
            return
        run = int(st.number_input('Номер окна', min_value=1, max_value=session['runs'], value=session['runs'],
                                  step=1, key='live_run_' + uid)) - 1
    st.plotly_chart(history_chart(instance.snapshot(store, uid, selected, run)), use_container_width=True,
                    key='live_history_chart_' + uid)
    st.caption('Состояние детектора сохраняется между окнами. Повторные точки не обучают детектор заново. '
               'История ограничена настройками хранения; сохранённые расчёты доступны по номеру окна. '
               'Время графиков: Europe/Moscow.')


def render(ctx):
    st.title('Рантайм реакций детекторов')
    if not isinstance(ctx.store, AccountStore):
        st.info('Войдите в рабочую учётку и добавьте сохранённые метрики в сет на странице «Каталог метрик».')
        return
    store = ctx.store
    workspace, revision = store.snapshot()
    st.caption('Сеты объединяют зарегистрированные метрики и детекторы. Детекторы продолжают обучение между окнами. '
               'Расчёт продолжается при переходе на другую страницу и закрытии браузера, пока работает процесс приложения. '
               'Ряды и реакции хранятся в памяти до его перезапуска; настройки сетов сохраняются в учётке.')
    if not catalog_metrics(workspace):
        st.info('Сначала сохраните метрики в каталоге или импортируйте их на странице «Детекторы».')
        sessions_panel(store)
        return
    with st.expander('Настройка сетов', expanded=not workspace.get('metric_sets')):
        set_editor(store, workspace, revision)
    sets = {s['id']: s for s in workspace.get('metric_sets', [])}
    chosen = st.multiselect('Сеты для запуска', list(sets), format_func=lambda uid: sets[uid]['name'])
    if st.button('Запустить выбранные сеты', type='primary', disabled=not chosen):
        for uid in chosen:
            try:
                live_runtime().start(store, uid)
            except (ValueError, TypeError, ArithmeticError) as exc:
                st.error(sets[uid]['name'] + ': ' + str(exc))
    sessions_panel(store)
