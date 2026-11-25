"""Source selectors and saved query metrics share a dedicated catalog page."""
import json
import time

import streamlit as st

from atropos_ui.clients import METRIC_SEARCH_LIMIT, METRIC_SEARCH_MIN_LENGTH, ApiError
from atropos_ui.components import render_result, time_window
from atropos_ui.metric_export import export_panel, query_entities
from atropos_ui.models import selector
from atropos_ui.query_state import open_query
from atropos_ui.set_widgets import add_to_set
from atropos_ui.time_ranges import format_bounds


def _preview(ctx, source, query, *, definition=None):
    scope = (source, query, definition)
    cached = st.session_state.get('catalog_preview')
    if cached and cached['scope'] != scope:
        st.session_state.pop('catalog_preview', None)
    selection, step = time_window('catalog_view', minimum_step=source.min_interval_seconds)
    a, b, c = st.columns(3)
    instant = a.button('Текущее значение')
    ranged = b.button('Построить временной ряд', type='primary')
    exporting = c.button('Экспорт метрики')
    needs_range = ranged or exporting
    if instant or needs_range:
        st.session_state.pop('catalog_preview', None)
        try:
            client = ctx.source_client(source)
            bounds = selection.resolve() if needs_range else None
            with st.spinner('Загружаем метрику…'):
                data = client.range(query, *bounds, step) if needs_range else client.instant(query)
                if definition is not None:
                    data = definition.transform_result(data)
            st.session_state['catalog_preview'] = {
                'scope': scope, 'data': data, 'window': (selection, step) if needs_range else None, 'bounds': bounds,
                'export_ready': exporting,
            }
        except (ApiError, ValueError) as exc:
            st.error(str(exc))
    cached = st.session_state.get('catalog_preview')
    if cached:
        if cached['window'] and cached['window'] != (selection, step):
            st.warning('Период изменился. Постройте график заново.')
        if cached.get('bounds'):
            st.caption('Данные за ' + format_bounds(cached['bounds'], cached['window'][0].timezone)
                       + ' · ' + cached['window'][0].timezone)
        try:
            render_result(cached['data'])
            export_panel(ctx.store, query_entities(cached['data'], definition.name if definition else 'query_result'),
                         key='catalog_preview_export', expanded=cached.get('export_ready', False))
        except ApiError as exc:
            st.error(str(exc))


def _source_catalog(ctx, source):
    search = st.text_input('Поиск метрик', key='catalog_source_search',
                           placeholder=f'Введите минимум {METRIC_SEARCH_MIN_LENGTH} символа').strip()
    refresh = st.button('Обновить результаты', disabled=len(search) < METRIC_SEARCH_MIN_LENGTH)
    if st.session_state.get('catalog_search') != search or refresh:
        for key in ('catalog_names', 'catalog_series', 'catalog_preview'):
            st.session_state.pop(key, None)
        st.session_state['catalog_search'] = search
    if len(search) < METRIC_SEARCH_MIN_LENGTH:
        st.info(f'Введите минимум {METRIC_SEARCH_MIN_LENGTH} символа имени метрики для поиска в источнике.')
        return
    client = ctx.source_client(source)
    if 'catalog_names' not in st.session_state:
        st.session_state['catalog_names'] = []
        try:
            with st.spinner('Поиск метрик в источнике…'):
                st.session_state['catalog_names'] = client.names(search)
        except (ApiError, ValueError) as exc:
            st.error(str(exc))
            return
    found = st.session_state['catalog_names']
    if len(found) > METRIC_SEARCH_LIMIT:
        st.warning(f'Показаны первые {METRIC_SEARCH_LIMIT} метрик. Уточните поиск, чтобы увидеть остальные.')
    names = found[:METRIC_SEARCH_LIMIT]
    if not names:
        st.info('Метрики не найдены. Измените поисковый запрос.')
        return
    metric = st.selectbox('Метрика', names)
    if st.button('Загрузить теги и ряды'):
        st.session_state.pop('catalog_series', None)
        try:
            now = time.time()
            st.session_state['catalog_series'] = (metric, client.series(metric, now - 3600, now))
        except ApiError as exc:
            st.error(str(exc))
    labels = None
    series = st.session_state.get('catalog_series')
    if series and series[0] == metric:
        st.caption(f'Найдено рядов за последний час: {len(series[1])}')
        selected = st.selectbox('Конкретный ряд', [None, *series[1]],
                                format_func=lambda row: 'Все ряды' if row is None else json.dumps(row, ensure_ascii=False, sort_keys=True))
        if selected:
            labels = {key: value for key, value in selected.items() if key != '__name__'}
    query = selector(metric, labels)
    st.code(query, language='promql')
    if st.button('Открыть в запросе'):
        open_query(source, query)
    _preview(ctx, source, query)


def _saved_catalog(ctx, source, pending):
    items = {item.name: item for item in ctx.store.definitions() if item.datasource_id == source.id}
    search = st.text_input('Поиск сохранённых метрик')
    names = [name for name in items if search.casefold() in name.casefold()]
    if not names:
        st.info('Сохранённых метрик нет. Постройте запрос и нажмите «Добавить в каталог».')
        return
    if pending.get('name') in names:
        st.session_state['catalog_saved_name'] = pending['name']
    elif st.session_state.get('catalog_saved_name') not in names:
        st.session_state['catalog_saved_name'] = names[0]
    name = st.selectbox('Сохранённая метрика', names, key='catalog_saved_name')
    item = items[name]
    if item.description:
        st.caption(item.description)
    st.code(item.query, language='promql')
    st.caption('Удаляемые теги: ' + (', '.join(item.drop_labels) or 'нет'))
    if item.extra_labels:
        st.dataframe([{'Тег': key, 'Значение': value} for key, value in item.extra_labels.items()], hide_index=True)
    add_to_set(ctx.store, item)
    st.caption('Период просмотра выбирается ниже и в каталог не сохраняется.')
    if st.button('Открыть в запросе'):
        open_query(source, item.query, item)
    _preview(ctx, source, item.query, definition=item)
    st.divider()
    confirmed = st.checkbox('Подтверждаю удаление метрики из каталога')
    if st.button('Удалить из каталога', disabled=not confirmed):
        try:
            ctx.store.delete('definition', name)
        except ValueError as exc:
            st.error(str(exc))
            return
        st.session_state.pop('catalog_preview', None)
        st.rerun()


def render(ctx):
    st.title('Каталог метрик и конструктор селектора')
    sources = [source for source in ctx.datasources.list() if source.prometheus_compatible]
    if not sources:
        st.info('Добавьте VictoriaMetrics или Prometheus на странице источников данных.')
        return
    pending = st.session_state.pop('pending_catalog', {})
    if pending.get('datasource_id'):
        st.session_state['catalog_source'] = next((source for source in sources if source.id == pending['datasource_id']), sources[0])
    elif st.session_state.get('catalog_source') not in sources:
        st.session_state['catalog_source'] = sources[0]
    source = st.selectbox('Источник', sources, format_func=lambda value: value.name, key='catalog_source')
    if st.session_state.get('catalog_scope') != source:
        for key in ('catalog_names', 'catalog_series', 'catalog_preview', 'catalog_search'):
            st.session_state.pop(key, None)
        st.session_state['catalog_scope'] = source
    if pending.get('name'):
        st.session_state['catalog_mode'] = 'Сохранённые метрики'
    mode = st.radio('Раздел каталога', ['Метрики источника', 'Сохранённые метрики'], horizontal=True, key='catalog_mode')
    if mode == 'Метрики источника':
        _source_catalog(ctx, source)
    else:
        _saved_catalog(ctx, source, pending)
