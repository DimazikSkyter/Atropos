"""Common set membership, bounded name search and result charts."""
import copy
import json
import re
from uuid import uuid4

import pandas as pd
import streamlit as st

from atropos_ui.metric_export import export_panel, result_entities
from atropos_ui.result_charts import result_figures
from atropos_ui.service_api import (
    AccountStore,
    catalog_metrics,
    definition_id,
    detector_instances,
)


def add_to_set(store, definition):
    if not isinstance(store, AccountStore):
        return
    workspace, revision = store.snapshot()
    sets = {s['id']: s for s in workspace.get('metric_sets', [])}
    uid = definition_id(definition.name)
    existing = [s['name'] for s in sets.values() if any(m['metric_id'] == uid for m in s['members'])]
    st.caption('В сетах: ' + (', '.join(existing) if existing else 'пока не добавлена'))
    with st.expander('Добавить метрику в сет'):
        target = st.selectbox('Сет для метрики', [None, *sets],
            format_func=lambda value: 'Новый сет' if value is None else sets[value]['name'],
            key='catalog_target_set_' + uid)
        name = st.text_input('Имя нового сета', key='catalog_new_set_' + uid) if target is None else None
        if st.button('Добавить в сет', key='catalog_add_set_' + uid):
            try:
                item = copy.deepcopy(sets[target]) if target else {
                    'id': uuid4().hex, 'name': name, 'start': 'now-5m', 'end': 'now',
                    'refresh_seconds': 15, 'members': []}
                if not any(m['metric_id'] == uid for m in item['members']):
                    item['members'].append({'metric_id': uid, 'detectors': {}, 'peers': []})
                store.save_metric_set(item, expected_revision=revision)
                st.success('Метрика добавлена в сет «' + item['name'] + '».')
            except ValueError as exc:
                st.error(str(exc))


def member_metrics(workspace, item):
    ids = {m['metric_id'] for m in item['members']}
    return [m for m in catalog_metrics(workspace) if m['id'] in ids]


def entity_options(workspace, item):
    metrics = {m['id']: m for m in member_metrics(workspace, item)}
    result = [{'id': 'm:' + uid, 'name': m['name'], 'kind': 'Метрика'} for uid, m in metrics.items()]
    for member in item['members']:
        metric = metrics[member['metric_id']]
        for binding in detector_instances(member['detectors']):
            name = binding['name'] if binding['id'] != binding['kind'] else metric['name'] + '_' + binding['kind']
            result.append({'id': 'r:' + metric['id'] + ':' + binding['id'], 'name': name, 'kind': 'Реакция'})
    return result


def select_names(options, *, key, label='Элементы сета', allow_all=False):
    by_id = {item['id']: item for item in options}
    modes = ['По имени', 'Регулярное выражение']
    if allow_all:
        modes.insert(0, 'Все элементы')
    mode = st.radio('Способ выбора', modes, horizontal=True, key=key + '_mode')
    if mode == 'Все элементы':
        matches = list(by_id)
    elif mode == 'Регулярное выражение':
        pattern = st.text_input('Регулярное выражение', key=key + '_regex')
        if not pattern:
            return []
        try:
            regex = re.compile(pattern)
            matches = [uid for uid, item in by_id.items() if regex.search(item['name'])]
        except re.error:
            st.error('Некорректное регулярное выражение.')
            return []
    else:
        search = st.text_input('Поиск по имени (от 3 символов)', key=key + '_search').strip()
        state_key = key + '_selected'
        previous = [uid for uid in st.session_state.get(state_key, []) if uid in by_id]
        if len(search) < 3:
            st.caption('Введите минимум 3 символа для подсказок.')
            matches = previous
        else:
            found = [uid for uid, item in by_id.items() if search.casefold() in item['name'].casefold()]
            if len(found) > 200:
                st.caption('Показаны первые 200 совпадений. Уточните имя.')
            matches = list(dict.fromkeys([*previous, *found[:200]]))
        st.session_state[state_key] = previous
        return st.multiselect(label, matches, format_func=lambda uid: by_id[uid]['name'], key=state_key)
    if len(matches) > 200:
        st.warning('Выбрано больше 200 элементов. Уточните регулярное выражение или имя.')
        return []
    return matches


def show_result(store, result, selected=None, *, key):
    if not result:
        st.info('Нет сохранённого расчёта для этого сета.')
        return
    from atropos_ui.live_runtime import prepare_set
    _, _, current = prepare_set(store.snapshot()[0], result['set_id'])
    if result.get('configuration_fingerprint') != current:
        st.warning('Состав сета, метрики или детекторы изменились. Показан предыдущий расчёт; рассчитайте сет заново.')
    st.caption('Расчёт: ' + str(pd.to_datetime(result['finished_at'], unit='s', utc=True).tz_convert('Europe/Moscow')))
    if result.get('bounds_ns'):
        first, last = (str(pd.Timestamp(t, unit='ns', tz='UTC').tz_convert('Europe/Moscow'))
                       for t in result['bounds_ns'])
        st.caption(first + ' → ' + last)
    for metric in result['metrics'].values():
        for output in metric['detectors'].values():
            if output.get('message'):
                st.warning(metric['name'] + ' · ' + output.get('name', '') + ': ' + output['message'])
            elif output.get('status') == 'warmup':
                name = output.get('name', metric['name'])
                points = max((len(s['scores']) for s in output.get('series', [])), default=0)
                required = output.get('parameters', {}).get('data_max_size') if output.get('detector') == 'Trend' else None
                detail = f'Получено {points} из {required} точек.' if required else f'Получено точек: {points}.'
                st.info(name + ': прогрев. ' + detail + ' Рассчитанной реакции пока нет; пропуски не означают нулевой score.')
            elif any(sum(value is not None for value in series['scores']) == 1 for series in output.get('series', [])):
                name = output.get('name', metric['name'])
                points = sum(len(series['scores']) for series in output['series'])
                ready = sum(value is not None for series in output['series'] for value in series['scores'])
                st.info(name + f': готовых оценок {ready} из {points}. '
                        'Одиночная оценка показана точкой; пропуски — отсутствие рассчитанной реакции.')
    entities = result_entities(result)
    if selected is not None:
        chosen = set(selected)
        chosen.update(k for e in entities if e['key'] in selected for k in e.get('source_keys', []))
        entities = [e for e in entities if e['key'] in chosen]
    pattern = st.text_input('Фильтр тегов рядов (регулярное выражение)', key=key + '_labels')
    if pattern:
        try:
            compiled = re.compile(pattern)
            entities = [e for e in entities if compiled.search(json.dumps(e['labels'], ensure_ascii=False, sort_keys=True))]
        except re.error:
            st.error('Некорректное регулярное выражение тегов.')
            return
    if len(entities) > 200:
        st.warning('Найдено больше 200 рядов. Уточните выбор метрик или фильтр тегов для отображения и экспорта.')
        return
    for kind, title, chart in result_figures(entities, bounds_ns=result.get('bounds_ns'), key=key):
        st.subheader(title)
        st.plotly_chart(chart, use_container_width=True, key=key + '_' + kind)
    if not entities:
        st.info('В выбранном окне нет данных для выбранных элементов.')
    export_panel(store, entities, key=key + '_export', revision=result['revision'])
