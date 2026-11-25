"""Set-first detector attachment and evaluation UI."""
import copy
import re
from uuid import uuid4

import streamlit as st

from atropos_ui.clients import ApiError
from atropos_ui.components import time_window
from atropos_ui.detector_runtime import DETECTORS
from atropos_ui.run_info import metadata_panel
from atropos_ui.service_api import detector_instances
from atropos_ui.set_ui import set_runtime
from atropos_ui.set_widgets import (
    entity_options,
    member_metrics,
    select_names,
    show_result,
)


def attach_detectors(item, metrics, selected, kinds, peers):
    result = copy.deepcopy(item)
    used = {b['name'] for m in result['members'] for b in detector_instances(m['detectors'])}
    used.update(m['name'] for m in metrics)
    by_id = {m['id']: m for m in metrics}
    for member in result['members']:
        if member['metric_id'] not in selected:
            continue
        metric = by_id[member['metric_id']]
        base = re.sub(r'[^a-zA-Z0-9_:]', '_', metric['name'])
        if not base or base[0].isdigit():
            base = 'metric_' + base
        for kind in kinds:
            proposed, index = base + '_' + kind, 1
            name = proposed
            while name in used:
                index += 1
                name = proposed + '_' + str(index)
            used.add(name)
            parameters = {'drift_mode': 'adaptive'} if kind == 'CUSUM' else {}
            if kind == 'Trend':
                parameters = {'window': 20, 'data_max_size': 60}
            member['detectors'][uuid4().hex] = {'kind': kind, 'name': name, 'parameters': parameters}
        if 'Correlation' in kinds:
            member['peers'] = list(dict.fromkeys([*member['peers'], *[p for p in peers if p != member['metric_id']]]))
    return result


def binding_editor(store, workspace, revision, item):
    metrics = member_metrics(workspace, item)
    options = [{'id': m['id'], 'name': m['name']} for m in metrics]
    selected = select_names(options, key='attach_' + item['id'], label='Метрики для детекторов')
    kinds = st.multiselect('Детекторы для прикрепления', list(DETECTORS), key='attach_kinds_' + item['id'])
    peers = []
    if 'Correlation' in kinds:
        names = {m['id']: m['name'] for m in metrics}
        peers = st.multiselect('Связанные метрики для Correlation', list(names),
                               format_func=lambda uid: names[uid], key='attach_peers_' + item['id'])
    st.caption('Каждое прикрепление создаёт отдельную именованную реакцию. '
               'CUSUM использует адаптацию фона; Trend — историю 60 точек и окно 20 точек. '
               'Редактор параметров появится отдельно.')
    if st.button('Прикрепить детекторы', disabled=not selected or not kinds, type='primary'):
        try:
            saved = attach_detectors(item, metrics, selected, kinds, peers)
            store.save_metric_set(saved, expected_revision=revision)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    rows, removable = [], {}
    names = {m['id']: m['name'] for m in metrics}
    for member in item['members']:
        for binding in detector_instances(member['detectors']):
            name = binding['name'] if binding['id'] != binding['kind'] else names[member['metric_id']] + '_' + binding['kind']
            key = member['metric_id'] + ':' + binding['id']
            removable[key] = name
            mode = ('Адаптация фона' if binding['parameters'].get('drift_mode') == 'adaptive'
                    else 'Фиксированный drift') if binding['kind'] == 'CUSUM' else 'По умолчанию'
            rows.append({'Метрика': names[member['metric_id']], 'Реакция': name,
                         'Детектор': binding['kind'], 'Режим': mode})
    if rows:
        st.dataframe(rows, hide_index=True, use_container_width=True)
        with st.expander('Удалить привязки детекторов'):
            removed = st.multiselect('Реакции для удаления', list(removable),
                format_func=lambda uid: removable[uid], key='remove_bindings_' + item['id'])
            if st.button('Убрать выбранные детекторы', disabled=not removed):
                saved = copy.deepcopy(item)
                for member in saved['members']:
                    member['detectors'] = {uid: value for uid, value in member['detectors'].items()
                                           if member['metric_id'] + ':' + uid not in removed}
                store.save_metric_set(saved, expected_revision=revision)
                st.rerun()
    with st.expander('Состав сета'):
        st.dataframe([{'Метрика': m['name'], 'Источник': m['datasource_id'],
                       'Тип': 'PromQL' if m.get('kind') == 'promql' else 'InfluxDB'} for m in metrics], hide_index=True)
        st.caption('Добавляйте сохранённые метрики на вкладке «Каталог метрик».')
        removed = st.multiselect('Убрать метрики из сета', list(names), format_func=lambda uid: names[uid],
                                 key='remove_members_' + item['id'])
        if st.button('Изменить состав сета', disabled=not removed):
            saved = copy.deepcopy(item)
            saved['members'] = [{**m, 'peers': [p for p in m['peers'] if p not in removed]}
                                for m in saved['members'] if m['metric_id'] not in removed]
            try:
                store.save_metric_set(saved, expected_revision=revision)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


@st.fragment(run_every='3s')
def results_panel(store, workspace, item):
    try:
        instance = set_runtime()
        state = instance.state(store, item['id'])
        if state and state['running']:
            st.info(state['progress'])
        elif state and state['error']:
            st.error(state['error'])
        result = instance.result(store, item['id'])
        if result:
            options = entity_options(workspace, item)
            chosen = select_names(options, key='calculated_' + item['id'], allow_all=True)
            show_result(store, result, chosen, key='set_result_' + item['id'])
            with st.expander('Параметры последнего расчёта'):
                metadata_panel(result)
    except (ApiError, ValueError) as exc:
        st.error(str(exc))


def render(store):
    workspace, revision = store.snapshot()
    sets = {s['id']: s for s in workspace.get('metric_sets', [])}
    if not sets:
        st.info('Сохраните метрику в каталоге и добавьте её в новый или существующий сет.')
        return
    uid = st.selectbox('Сет', list(sets), format_func=lambda value: sets[value]['name'], key='detectors_set')
    item = sets[uid]
    st.subheader('Метрики и детекторы сета')
    binding_editor(store, workspace, revision, item)
    st.subheader('Применить детекторы')
    mode = st.radio('Данные для расчёта', ['Окно из источников', 'Сохранённые серии'], horizontal=True)
    if mode == 'Окно из источников':
        minimum = max((s.get('min_interval_seconds', 1) for s in workspace.get('sources', [])
                       if s['id'] in {m['datasource_id'] for m in member_metrics(workspace, item)}), default=1)
        selection, step = time_window('detectors_set_window', minimum_step=minimum, fixed=True)
    else:
        st.caption('Используется последний сохранённый в PostgreSQL набор исходных серий этого сета. '
                   'Источники не запрашиваются. Можно применить вновь прикреплённые детекторы.')
        selection, step = None, 30
    if st.button('Рассчитать сет', type='primary'):
        try:
            first, last = selection.resolve() if selection else (0, 1)
            if not set_runtime().start(store, uid, int(round(first * 1e9)), int(round(last * 1e9)), step,
                                       'source' if selection else 'saved'):
                st.info('Расчёт этого сета уже выполняется.')
        except (ApiError, ValueError) as exc:
            st.error(str(exc))
    results_panel(store, workspace, item)
