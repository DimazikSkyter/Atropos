import streamlit as st

from atropos_ui.clients import ApiError
from atropos_ui.components import query_frame, render_result, time_window
from atropos_ui.models import MetricDefinition
from atropos_ui.query_state import open_catalog
from atropos_ui.time_picker import restore_window
from atropos_ui.time_ranges import format_bounds


def _draft(sources):
    draft = st.session_state.setdefault('query_draft', {})
    pending = st.session_state.pop('pending_query', None)
    if pending is not None:
        window = draft.get('window')
        draft = dict(pending)
        if window:
            draft['window'] = window
        st.session_state['query_draft'] = draft
        for key in ('query_source', 'promql', 'query_metric_name', 'query_description', 'query_drop_labels', 'query_result'):
            st.session_state.pop(key, None)
    if st.session_state.get('query_source') not in sources:
        st.session_state['query_source'] = next((source for source in sources if source.id == draft.get('datasource_id')), sources[0])
    st.session_state.setdefault('promql', draft.get('query', 'up'))
    if 'window' in draft:
        restore_window('source', draft['window'])
    return draft


def _metric_options(draft, source, result):
    st.subheader('Имя и теги метрики')
    data = result['data']
    rows = data.get('result', []) if data.get('resultType') in {'matrix', 'vector'} else []
    names = {row['metric'].get('__name__') for row in rows} - {None, ''}
    default_name = next(iter(names)) if len(names) == 1 else 'my_metric'
    st.session_state.setdefault('query_metric_name', draft.get('name', default_name))
    st.session_state.setdefault('query_description', draft.get('description', ''))
    name = st.text_input('Имя метрики', key='query_metric_name')
    description = st.text_input('Описание', key='query_description')
    available = {key for row in rows for key in row['metric'] if not key.startswith('__')}
    st.session_state.setdefault('query_drop_labels', list(draft.get('drop_labels', ())))
    available.update(st.session_state['query_drop_labels'])
    dropped = st.multiselect('Удалить теги', sorted(available), key='query_drop_labels', placeholder='Выберите теги')
    extras = draft.setdefault('extra_labels', {})
    a, b, c = st.columns([2, 3, 1], vertical_alignment='bottom')
    new_key = a.text_input('Новый тег', key='query_new_label')
    new_value = b.text_input('Значение тега', key='query_new_label_value')
    if c.button('Добавить тег'):
        try:
            MetricDefinition(name, source.id, result['query'], drop_labels=tuple(dropped), extra_labels={new_key: new_value})
            extras[new_key] = new_value
        except ValueError as exc:
            st.error(str(exc))
    for key, value in list(extras.items()):
        left, right = st.columns([5, 1])
        left.write(f'{key}: {value}')
        if right.button('Убрать тег ' + key, key='query_remove_label_' + key):
            extras.pop(key)
            st.rerun()
    draft.update(name=name, description=description, drop_labels=tuple(dropped))
    try:
        item = MetricDefinition(name, source.id, result['query'], description,
                                drop_labels=tuple(dropped), extra_labels=dict(extras))
        return item, item.transform_result(data)
    except ValueError as exc:
        st.error(str(exc))
        return None, None


def render(ctx):
    st.title('PromQL · рабочая область')
    sources = [source for source in ctx.datasources.list() if source.prometheus_compatible]
    if not sources:
        st.info('Для PromQL добавьте VictoriaMetrics или Prometheus на странице источников данных.')
        return
    draft = _draft(sources)
    source = st.selectbox('Источник', sources, format_func=lambda value: value.name, key='query_source')
    if draft.get('datasource_id') and draft['datasource_id'] != source.id:
        for key in ('catalog_name', 'name', 'description', 'drop_labels', 'extra_labels'):
            draft.pop(key, None)
        for key in ('query_metric_name', 'query_description', 'query_drop_labels'):
            st.session_state.pop(key, None)
    draft['datasource_id'] = source.id
    if st.session_state.get('query_scope') != source:
        st.session_state.pop('query_result', None)
        st.session_state['query_scope'] = source
    if st.button('Открыть каталог метрик'):
        open_catalog(source)
    query = st.text_area('PromQL / MetricsQL', height=130, key='promql')
    draft['query'] = query
    st.caption('MetricsQL поддерживается VictoriaMetrics. Для выбора метрики и тегов откройте каталог.')
    selection, step = time_window('source', minimum_step=source.min_interval_seconds)
    draft['window'] = {'range': selection, 'step': step}
    a, b = st.columns(2)
    instant = a.button('Текущее значение', use_container_width=True)
    ranged = b.button('Построить временной ряд', type='primary', use_container_width=True)
    if instant or ranged:
        st.session_state.pop('query_result', None)
        try:
            client = ctx.source_client(source)
            bounds = selection.resolve() if ranged else None
            with st.spinner('Выполняем запрос…'):
                data = client.range(query, *bounds, step) if ranged else client.instant(query)
            st.session_state['query_result'] = {'query': query, 'data': data, 'scope': source,
                                                 'window': (selection, step) if ranged else None, 'bounds': bounds}
        except (ApiError, ValueError) as exc:
            st.error(str(exc))
    feedback = st.session_state.pop('query_feedback', None)
    if feedback:
        st.success(feedback)
    result = st.session_state.get('query_result')
    if not result:
        return
    try:
        query_frame(result['data'])
    except ApiError as exc:
        st.error(str(exc))
        return
    st.subheader('Результат запроса')
    st.code(result['query'], language='promql')
    matches = result['query'] == query and result['scope'] == source
    if not matches or (result['window'] and result['window'] != (selection, step)):
        st.warning('Параметры изменились. Результат относится к предыдущему запросу.')
    if result.get('bounds'):
        st.caption('Данные за ' + format_bounds(result['bounds'], result['window'][0].timezone)
                   + ' · ' + result['window'][0].timezone)
    if not result['data'].get('result') or result['data'].get('resultType') == 'string':
        try:
            render_result(result['data'])
        except ApiError as exc:
            st.error(str(exc))
        return
    item, transformed = _metric_options(draft, source, result)
    if transformed is not None:
        try:
            render_result(transformed)
        except ApiError as exc:
            st.error(str(exc))
            item = None
    previous = draft.get('catalog_name')
    label = 'Обновить в каталоге' if previous else 'Добавить в каталог'
    if st.button(label, type='primary', disabled=item is None or not matches):
        try:
            ctx.store.save_definition(item, previous_name=previous)
            draft['catalog_name'] = item.name
            st.session_state['query_feedback'] = 'Метрика сохранена в каталоге. Период просмотра не сохраняется.'
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if previous and st.button('Добавить копию в каталог', disabled=item is None or not matches):
        try:
            ctx.store.save_definition(item)
            draft['catalog_name'] = item.name
            st.session_state['query_feedback'] = 'Копия метрики добавлена в каталог.'
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    st.caption('Сохраняются запрос, имя и изменения тегов. Данные и выбранный период в каталог не записываются.')
    if previous and st.button('Открыть в каталоге'):
        open_catalog(source, previous)
