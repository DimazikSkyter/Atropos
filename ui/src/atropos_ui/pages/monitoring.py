"""Account-scoped monitoring of sums and traceable detector reaction components."""
import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from atropos_ui.clients import ApiError
from atropos_ui.pages.detectors import progress_panel
from atropos_ui.reaction_export import csv_export, write_influx
from atropos_ui.replay_ui import runtime
from atropos_ui.service_api import (
    AccountStore,
    aggregate_components,
    collect_components,
    group_labels,
    select_components,
)

COLORS = ['#2563b9', '#299246', '#9050b7', '#c46810', '#d84b5c', '#16a6a0']


def reaction_chart(groups):
    figure = go.Figure()
    # Use a common grid without interpolation or carrying old reactions forward.
    times = sorted({t for group in groups for t in group['times']})
    # Put the strongest contribution on top, as in the monitoring reference.
    ordered = sorted(groups, key=lambda g: (max((v for v in g['scores'] if v is not None), default=0), g['name']))
    for index, group in enumerate(ordered):
        values = dict(zip(group['times'], zip(group['scores'], group['coverage'])))
        figure.add_trace(go.Scatter(x=pd.to_datetime(times, unit='ns', utc=True).tz_convert('Europe/Moscow'),
            y=[values.get(t, (None, 0))[0] for t in times],
            customdata=[values.get(t, (None, 0))[1] for t in times],
            name=group['name'], mode='lines', stackgroup='reactions', connectgaps=False,
            fillcolor=COLORS[index % len(COLORS)],
            line=dict(width=1, color=COLORS[index % len(COLORS)]),
            hovertemplate='%{y:.4f}<br>Рассчитано компонентов: %{customdata}<extra>%{fullData.name}</extra>'))
    figure.update_layout(height=520, hovermode='x unified',
        xaxis_title='Время · Europe/Moscow', yaxis_title='Сумма реакций детекторов',
        legend=dict(orientation='h', y=-0.2, traceorder='normal', itemclick='toggle', itemdoubleclick='toggleothers'),
        margin=dict(l=20, r=20, t=20, b=100))
    figure.update_yaxes(rangemode='tozero')
    return figure


def filters(components):
    keys = sorted({k for c in components for s in c['sources'] for k in s['labels']})
    left, right = st.columns([1, 2])
    with left:
        label = st.selectbox('Лейбл для фильтра', [None, *keys],
            index=keys.index('service') + 1 if 'service' in keys else 0,
            format_func=lambda k: 'Все лейблы' if k is None else k, key='monitor_label')
        mode = st.radio('Условие', ['Регулярное выражение', 'Точные значения'], horizontal=True)
    exact, regex, selected = {}, {}, None
    with right:
        if mode == 'Регулярное выражение':
            pattern = st.text_input('Регулярное выражение значения', value='.+', key='monitor_pattern',
                                    help='Полное совпадение. .+ — любой непустой лейбл; .* — включая пустое значение.')
            if label is not None:
                regex[label] = pattern
        elif label is not None:
            values = sorted({s['labels'][label] for c in components for s in c['sources'] if label in s['labels']})
            selected = st.multiselect('Значения лейбла', values, default=values, key='monitor_values_' + label)
        st.caption('Отсутствующий лейбл не совпадает с регуляркой. Все условия объединяются через И.')
    with st.expander('Дополнительные условия по лейблам'):
        exact_json = st.text_area('Точные лейблы (JSON)', '{}', key='monitor_exact',
                                  placeholder='{"environment": "production"}')
        regex_json = st.text_area('Регулярки лейблов (JSON)', '{}', key='monitor_regex',
                                  placeholder='{"host": "api-.*"}')
    available = sorted({c['detector'] for c in components})
    detectors = st.multiselect('Детекторы', available, default=available, key='monitor_detectors')
    grouping = st.multiselect('Группировать реакции по лейблам', list(dict.fromkeys([*keys, 'detector'])),
        default=[label] if label is not None else [], key='monitor_groups_' + str(label))
    try:
        extra_exact, extra_regex = json.loads(exact_json), json.loads(regex_json)
        if not isinstance(extra_exact, dict) or not isinstance(extra_regex, dict):
            raise ValueError('Дополнительные условия должны быть объектами JSON.')
        # Validate JSON types and regex even when no components pass the first filter.
        select_components([], labels=extra_exact, label_regex=extra_regex)
        chosen = select_components(components, labels=exact, label_regex=regex, detectors=detectors)
        chosen = select_components(chosen, labels=extra_exact, label_regex=extra_regex)
    except (ValueError, TypeError) as exc:
        st.error(str(exc))
        return None, grouping
    if selected is not None:
        chosen = [c for c in chosen if all(s['labels'].get(label) in selected for s in c['sources'])]
    return chosen, grouping


def detail_panel(groups, components, result, grouping):
    with st.expander('Компоненты реакции и исходные временные ряды'):
        group = st.selectbox('Группа реакции', groups, format_func=lambda g: g['name'])
        members = [c for c in components if group_labels(c, grouping) == group['labels']]
        st.dataframe([{'Детектор': c['detector'], 'ID метрики': c['metric_id'],
            'ID исходного ряда': c['source_series_id'],
            'Исходная метрика': ' ↔ '.join(s['name'] for s in c['sources']),
            'Максимум реакции': max((v for v in c['scores'] if v is not None), default=None)}
            for c in members], hide_index=True, use_container_width=True)
        component = st.selectbox('Компонент реакции', members,
            format_func=lambda c: c['detector'] + ' · ' + ' ↔ '.join(s['name'] for s in c['sources']) +
                                 ' · ' + c['source_series_id'])
        figure = go.Figure(go.Scatter(x=pd.to_datetime(component['times'], unit='ns', utc=True).tz_convert('Europe/Moscow'),
            y=component['scores'], mode='lines', name=component['detector'], connectgaps=False))
        figure.update_layout(height=280, yaxis_title='Реакция', xaxis_title='Время · Europe/Moscow')
        st.plotly_chart(figure, use_container_width=True, key='monitor_component_chart')
        original = go.Figure()
        for source in component['sources']:
            metric = result['metrics'].get(source['metric_id'], {})
            for series in metric.get('series', []):
                if series.get('labels', {}) == source['source_labels']:
                    original.add_trace(go.Scatter(x=pd.to_datetime(series['times'], unit='ns', utc=True).tz_convert('Europe/Moscow'),
                        y=series['values'], name=source['name'] + ' ' + series['label'], mode='lines'))
        if original.data:
            original.update_layout(height=300, yaxis_title='Исходное значение', hovermode='x unified',
                                   xaxis_title='Время · Europe/Moscow')
            st.plotly_chart(original, use_container_width=True, key='monitor_source_chart')
        st.json(component['sources'])


def export_panel(store, groups, components, revision):
    st.download_button('Скачать CSV', csv_export(groups, components), 'atropos-reactions.csv', 'text/csv',
                       key='monitor_csv')
    with st.expander('Экспорт в InfluxDB'):
        sources = [s for s in store.sources() if s.kind == 'InfluxDB' and s.influx_version == '2']
        if not sources:
            st.info('Добавьте подключение InfluxDB 2.x на странице «Источники данных».')
            return
        source = st.selectbox('Подключение для экспорта', sources, format_func=lambda s: s.name)
        measurement = st.text_input('Имя метрики реакций', 'atropos_detector_reactions')
        st.caption('Будут записаны суммы (_sum) и компоненты (_component) текущей выборки. '
                   'Компоненты содержат detector, metric_id и source_series_id. '
                   'Пропуски прогрева остаются в CSV и пропускаются в InfluxDB.')
        if st.button('Экспортировать реакции в InfluxDB', key='monitor_influx'):
            try:
                if store.snapshot()[1] != revision:
                    raise ValueError('Настройки изменились. Обновите страницу перед экспортом.')
                with st.spinner('Запись реакций в InfluxDB…'):
                    count = write_influx(source, groups, components, measurement)
                st.success(f'Экспортировано точек: {count}.')
            except (ApiError, ValueError) as exc:
                st.error(str(exc))


def legacy_render(ctx):
    st.title('Мониторинг реакций детекторов')
    if not isinstance(ctx.store, AccountStore):
        st.info('Войдите в рабочую учётку и импортируйте метрики на странице «Результаты детекторов».')
        return
    store = ctx.store
    st.caption('Сумма score всех подходящих метрик вашей учётки. '
               'Лейблы учитывают сохранённые удаления и дополнения.')
    if st.button('Обновить реакции из источников', type='primary'):
        if not runtime().start(store):
            st.info('Предыдущий прогон ещё выполняется.')
    progress_panel(store)
    result = runtime().result(store)
    if not result:
        st.info('Нет результатов. Импортируйте метрики и запустите пересчёт.')
        return
    if result['revision'] != store.snapshot()[1]:
        st.info('Настройки изменились. Дождитесь завершения нового пересчёта.')
        return
    st.caption('Последний прогон: ' + str(pd.to_datetime(result['finished_at'], unit='s', utc=True).tz_convert('Europe/Moscow')))
    components, warnings = collect_components(result)
    for message in warnings:
        st.warning(message)
    if not components:
        st.info('В прогоне нет реакций детекторов.')
        return
    chosen, grouping = filters(components)
    if chosen is None:
        return
    if not chosen:
        st.info('Нет реакций с выбранными лейблами и детекторами.')
        return
    mixed_sum = st.checkbox('Суммировать score разных детекторов', value=False,
        help='У алгоритмов разные шкалы. Такая сумма — прикладной индекс, а не вероятность аномалии.')
    if not mixed_sum and len({c['detector'] for c in chosen}) > 1 and 'detector' not in grouping:
        grouping = [*grouping, 'detector']
    groups = aggregate_components(chosen, grouping)
    left, middle, right = st.columns(3)
    left.metric('Исходных рядов', len({s['source_series_id'] for c in chosen for s in c['sources']}))
    middle.metric('Компонентов реакций', len(chosen))
    right.metric('Групп на графике', len(groups))
    st.plotly_chart(reaction_chart(groups), use_container_width=True, key='monitor_reactions_chart')
    st.caption('Score разных алгоритмов не имеют общей калибровки и не являются вероятностями. '
               'Суммируются score на точных исходных timestamps; сумма может быть больше 1. '
               'Прогрев и пропуски не входят в сумму; на графике отсутствующий вклад занимает нулевую площадь. '
               'Подсказка показывает число доступных компонентов. '
               'Correlation включается, если оба ряда проходят фильтр; одинаковая пара с одинаковыми параметрами учитывается один раз. '
               'Нажатие на легенду скрывает слой, двойной щелчок оставляет один.')
    if not any(v is not None for g in groups for v in g['scores']):
        st.info('Все выбранные компоненты находятся в прогреве; рассчитанных реакций пока нет.')
    detail_panel(groups, chosen, result, grouping)
    export_panel(store, groups, chosen, result['revision'])


def render(ctx):
    from atropos_ui.set_monitoring import render as render_sets
    render_sets(ctx)
