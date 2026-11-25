"""Monitoring for any owned set, with bounded suggestions and rolling windows."""
import time

import streamlit as st

from atropos_ui.clients import ApiError
from atropos_ui.components import time_window
from atropos_ui.service_api import AccountStore
from atropos_ui.set_ui import set_runtime
from atropos_ui.set_widgets import (
    entity_options,
    member_metrics,
    select_names,
    show_result,
)


def render(ctx):
    st.title('Мониторинг реакций')
    if not isinstance(ctx.store, AccountStore):
        st.info('Войдите в рабочую учётку, чтобы открыть свои сеты.')
        return
    store = ctx.store
    workspace, _ = store.snapshot()
    sets = {s['id']: s for s in workspace.get('metric_sets', [])}
    if not sets:
        st.info('Добавьте сохранённые метрики в сет на вкладке «Каталог метрик».')
        return
    uid = st.selectbox('Сет', list(sets), format_func=lambda value: sets[value]['name'], key='monitor_set')
    item = sets[uid]
    minimum = max((s.get('min_interval_seconds', 1) for s in workspace.get('sources', [])
                   if s['id'] in {m['datasource_id'] for m in member_metrics(workspace, item)}), default=1)
    selection, step = time_window('monitor_set_window', minimum_step=minimum)
    auto = st.checkbox('Автообновление', value=True)
    interval = st.number_input('Обновлять каждые, секунд', min_value=3, max_value=3600,
                               value=max(3, min(3600, int(item['refresh_seconds']))), key='monitor_interval_' + uid)
    if st.button('Обновить сейчас', type='primary'):
        st.session_state['monitor_force_' + uid] = True
    options = entity_options(workspace, item)
    selected = select_names(options, key='monitor_entities_' + uid, allow_all=True,
                            label='Метрики и реакции для отображения')

    @st.fragment(run_every=interval if auto else None)
    def view():
        try:
            instance = set_runtime()
            state = instance.state(store, uid)
            signature = (uid, selection.start, selection.end, selection.mode, selection.timezone, step)
            force = st.session_state.pop('monitor_force_' + uid, False)
            changed = st.session_state.get('monitor_view_signature') != signature
            due = state is None or time.time() - state['started_at'] >= interval
            if (force or (auto and (due or changed))) and not (state and state['running']):
                first, last = selection.resolve()
                instance.start(store, uid, int(round(first * 1e9)), int(round(last * 1e9)), step)
                st.session_state['monitor_view_signature'] = signature
                state = instance.state(store, uid)
            if state and state['running']:
                st.info('Обновляем выбранное окно: ' + state['progress'])
            elif state and state['error']:
                st.error(state['error'])
            result = instance.result(store, uid)
            show_result(store, result, selected, key='monitor_set_' + uid)
        except (ApiError, ValueError) as exc:
            st.error(str(exc))

    view()
