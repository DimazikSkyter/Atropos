import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from atropos_ui.clients import ApiError
from atropos_ui.detector_runtime import DETECTORS, defaults, validate_bindings
from atropos_ui.influx import descriptors, read_source
from atropos_ui.replay_ui import runtime
from atropos_ui.run_info import metadata_panel
from atropos_ui.service_api import AccountStore


def suggested_peers(metrics):
    """A visible, editable initial reference for singleton measurements."""
    for metric in metrics:
        if len(metric['series']) > 1:
            continue
        own = metric['series'][0]
        candidates = []
        for other in metrics:
            if other['id'] == metric['id']:
                continue
            overlap = max(min(own['end'], s['end']) - max(own['start'], s['start']) for s in other['series'])
            if overlap > 0:
                candidates.append((other['measurement'] == metric['measurement'], overlap, other['id']))
        if candidates:
            metric['peers'] = [max(candidates)[2]]
    return metrics


def import_panel(store):
    sources = [s for s in store.sources() if s.kind == 'InfluxDB' and s.influx_version == '2']
    if not sources:
        st.info('Добавьте собственное подключение InfluxDB 2.x на странице «Источники данных».')
        return
    source = st.selectbox('Источник InfluxDB', sources, format_func=lambda s: s.name)
    if st.button('Найти все метрики в InfluxDB'):
        _, revision = store.snapshot()
        try:
            with st.spinner('Чтение всей исходной истории без агрегации…'):
                data, skipped = read_source(source)
            found = suggested_peers(descriptors(data, DETECTORS))
            st.session_state['influx_discovery'] = (source.id, revision, found)
            if skipped:
                st.warning(f'Пропущено нечисловых/NaN/Inf точек: {skipped}.')
        except (ApiError, ValueError) as exc:
            st.error(str(exc))
    discovery = st.session_state.get('influx_discovery')
    if discovery and discovery[0] == source.id:
        _, revision, found = discovery
        by_id = {m['id']: m for m in found}
        st.caption(f'Найдено метрик (measurement + field): {len(found)}. '
                   'Один элемент включает все исходные наборы лейблов. Импорт действует только для вашей учётки.')
        selected = st.multiselect('Добавить в мой каталог', list(by_id), default=list(by_id),
                                 format_func=lambda uid: by_id[uid]['name'])
        if st.button('Импортировать выбранные метрики', type='primary', disabled=not selected):
            chosen = [{**by_id[uid], 'peers': [p for p in by_id[uid]['peers'] if p in selected]} for uid in selected]
            store.import_metrics(chosen, expected_revision=revision)
            del st.session_state['influx_discovery']
            runtime().start(store)
            st.rerun()


def editor(store, metric, metrics, revision):
    with st.expander('Имя, лейблы и привязка детекторов'):
        st.caption('Каждый детектор получает новый экземпляр для каждого ряда. '
                   'Для Correlation используются точные общие timestamps двух рядов; пропуски не заполняются.')
        with st.form('metric_config_' + metric['id'] + '_' + str(revision)):
            name = st.text_input('Имя метрики', value=metric['name'])
            label_keys = sorted({key for s in metric['series'] for key in s['labels']} | set(metric.get('drop_labels', [])))
            dropped = st.multiselect('Удалить исходные лейблы', label_keys, default=metric.get('drop_labels', []))
            extra = st.text_area('Дополнительные лейблы (JSON)', json.dumps(metric.get('extra_labels', {}), ensure_ascii=False, indent=2))
            enabled = st.multiselect('Закреплённые детекторы', list(DETECTORS), default=list(metric['detectors']))
            parameters = st.text_area('Параметры детекторов (JSON)', json.dumps(metric['detectors'], indent=2), height=200)
            peers = [m for m in metrics if m['id'] != metric['id']]
            by_id = {m['id']: m for m in peers}
            references = st.multiselect('Связанные метрики для Correlation', list(by_id), default=metric.get('peers', []),
                                       format_func=lambda uid: by_id[uid]['name'])
            if st.form_submit_button('Сохранить метрику и привязки', type='primary'):
                try:
                    overrides, labels = json.loads(parameters), json.loads(extra)
                    if not isinstance(overrides, dict) or not isinstance(labels, dict) or not name.strip():
                        raise ValueError('Укажите имя и объекты JSON для параметров и лейблов.')
                    if not all(isinstance(k, str) and k and isinstance(v, str) for k, v in labels.items()):
                        raise ValueError('Лейблы должны содержать строковые имена и значения.')
                    bindings = {key: overrides.get(key, {}) for key in enabled}
                    validate_bindings(bindings)
                    store.update_metric(metric['id'], {'name': name.strip(), 'extra_labels': labels,
                        'drop_labels': dropped, 'detectors': bindings, 'peers': references}, expected_revision=revision)
                    runtime().start(store)
                    st.rerun()
                except (ValueError, TypeError) as exc:
                    st.error(str(exc))
        with st.expander('Параметры по умолчанию'):
            st.json({name: defaults(name) for name in DETECTORS})
        if st.button('Убрать метрику из моей учётки', key='remove_' + metric['id']):
            store.remove_metric(metric['id'], expected_revision=revision)
            st.rerun()


@st.fragment(run_every='3s')
def progress_panel(store):
    instance = runtime()
    state = instance.state(store)
    if state and not state['future'].done():
        st.info('Пересчёт: ' + state['progress'])
    elif state and state['error']:
        st.error(state['error'])
    elif state and state['result'] is not None:
        stamp = (state['revision'], state['result']['finished_at'])
        if st.session_state.get('rendered_replay') != stamp:
            st.session_state['rendered_replay'] = stamp
            st.rerun(scope='app')


def display_labels(series):
    """Use the effective labels saved with this run, including user transforms."""
    return json.loads(series['label'])


def label_filter(metric, series):
    labels = [display_labels(item) for item in series]
    keys = sorted({key for item in labels for key in item})
    if not keys:
        return set(range(len(series))), None
    left, right = st.columns([1, 2])
    with left:
        label = st.selectbox('Лейбл для фильтра', [None, *keys],
                             index=keys.index('host') + 1 if 'host' in keys else 0,
                             format_func=lambda value: 'Без фильтра' if value is None else value,
                             key='label_key_' + metric['id'])
    if label is None:
        return set(range(len(series))), None
    values = sorted({item.get(label) for item in labels}, key=lambda value: (value is not None, value or ''))
    with right:
        selected = st.multiselect('Значения лейбла', values, default=values,
            format_func=lambda value: '(лейбл отсутствует)' if value is None else value,
            key='label_values_' + metric['id'] + '_' + label,
            placeholder='Выберите значения')
    visible = {index for index, item in enumerate(labels) if item.get(label) in selected}
    st.caption(f'Показано {len(visible)} из {len(series)} рядов. Фильтр меняет только отображение.')
    return visible, (label, selected)


def correlation_labels(pair, label_index):
    # Existing saved runs identify pairs by the exact generated display names.
    # Resolve both members against the source series, never by substring values.
    for name, labels in label_index.items():
        prefix = name + ' ↔ '
        if pair['label'].startswith(prefix):
            other = label_index.get(pair['label'][len(prefix):])
            if other is not None:
                return labels, other
    return None


def results_chart(metric, result):
    if not result:
        st.info('Запустите пересчёт, чтобы загрузить исходные данные и результаты.')
        return
    st.caption('Последний прогон: ' + str(pd.to_datetime(result['finished_at'], unit='s', utc=True).tz_convert('Europe/Moscow')))
    for message in result['warnings']:
        st.warning(message)
    summary = [{'Метрика': item['name'], 'Рядов': len(item['series']),
                'Детектор': name, 'Статус': output['status'],
                'Подробности': output.get('message', '')}
               for item in result['metrics'].values() for name, output in item['detectors'].items()]
    with st.expander('Сводка всех прогонов'):
        st.dataframe(summary, hide_index=True, use_container_width=True)
        for item in result['metrics'].values():
            if item['status'] == 'error':
                st.error(item['name'] + ': ' + item['message'])
    output = result['metrics'].get(metric['id'])
    if not output:
        st.info('Для этой метрики ещё нет результатов.')
        return
    if output['status'] == 'error':
        st.error(output['message'])
        return
    visible, active_filter = label_filter(metric, output['series'])
    if not visible:
        st.info('Нет рядов с выбранными значениями лейбла.')
        return
    options = list(output['detectors'])
    chosen = st.multiselect('Показать результаты детекторов', options,
                           default=[name for name in ('CUSUM', 'Trend') if name in options], key='visible_' + metric['id'])
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                           subplot_titles=('Исходные значения', 'Результаты детекторов'))
    for index, series in enumerate(output['series']):
        if index not in visible:
            continue
        figure.add_trace(go.Scattergl(x=pd.to_datetime(series['times'], unit='ns', utc=True), y=series['values'],
            name='Исходный · ' + series['label'], mode='lines'), row=1, col=1)
    label_index = {item['name'] + ' ' + series['label']: display_labels(series)
                   for item in result['metrics'].values() for series in item['series']}
    for name in chosen:
        calculated = output['detectors'][name]
        if calculated.get('message'):
            st.warning(name + ': ' + calculated['message'])
        elif calculated['status'] == 'warmup':
            st.info(name + ': недостаточно данных для прогрева или нет общих timestamps.')
        plotted = 0
        for index, series in enumerate(calculated.get('series', [])):
            if name != 'Correlation' and index not in visible:
                continue
            if name == 'Correlation' and active_filter:
                key, values = active_filter
                members = correlation_labels(series, label_index)
                if members is None or not all(labels.get(key) in values for labels in members):
                    continue
            times = series.get('times', output['series'][index]['times'] if name != 'Correlation' else [])
            figure.add_trace(go.Scattergl(x=pd.to_datetime(times, unit='ns', utc=True), y=series['scores'],
                name=name + ' · ' + series['label'], mode='lines'), row=2, col=1)
            plotted += 1
        if name == 'Correlation' and active_filter and not plotted and calculated.get('series'):
            st.info('Correlation: нет пар, у которых оба ряда проходят фильтр по лейблу.')
    figure.update_layout(height=750, legend=dict(itemclick='toggle', itemdoubleclick='toggleothers'),
                         uirevision=metric['id'] + str(result['revision']) +
                         json.dumps([sorted(visible), active_filter, chosen], ensure_ascii=False), hovermode='x unified')
    figure.update_yaxes(title_text='Значение', row=1, col=1)
    figure.update_yaxes(title_text='Score / обнаружение', row=2, col=1)
    # Matched x/x2 ranges alone do not share hover. Both panels must use x.
    figure.update_traces(xaxis='x')
    figure.update_layout(
        grid=dict(rows=2, columns=1, xaxes=['x'], yaxes=['y', 'y2']),
        hoversubplots='axis', hoverdistance=-1, spikedistance=-1,
        xaxis=dict(anchor='y2', matches=None, showticklabels=True, showspikes=True,
                   spikemode='across', spikesnap='cursor', spikedash='dot',
                   spikethickness=1, spikecolor='#A8B7C9'),
        yaxis2=dict(anchor='x'),
    )
    figure.layout.xaxis2 = None
    st.plotly_chart(figure, use_container_width=True, key='detector_chart_' + metric['id'])
    st.caption('Нажмите на легенду, чтобы скрыть или включить линию; двойной щелчок оставляет одну линию. '
               'Прогрев показан пропуском. RuntimeTrend показывает 0/1; остальные детекторы — собственный score.')


def results_panel(store, metric):
    result = runtime().result(store)
    charts, metadata = st.tabs(['Результаты', 'Параметры запуска'])
    with charts:
        results_chart(metric, result)
    with metadata:
        if result:
            metadata_panel(result, metric['id'])
        else:
            st.info('Нет завершённого прогона.')


def legacy_render(ctx):
    st.subheader('Импорт InfluxDB и прежние результаты')
    if not isinstance(ctx.store, AccountStore):
        st.info('Для прогона реальных метрик войдите в рабочую учётную запись.')
        return
    store = ctx.store
    st.caption('Личный каталог: метрики и настройки других учётных записей здесь недоступны.')
    with st.expander('Импорт метрик из InfluxDB', expanded=not store.metrics()):
        import_panel(store)
    workspace, revision = store.snapshot()
    metrics = workspace.get('metrics', [])
    if not metrics:
        return
    st.metric('Метрик в моей учётке', len(metrics))
    if st.button('Пересчитать все мои метрики', type='primary'):
        if not runtime().start(store):
            st.info('Предыдущий прогон ещё выполняется.')
    metric = st.selectbox('Закреплённая метрика', metrics, format_func=lambda m: m['name'])
    st.caption(f'Исходник: {metric["measurement"]} / {metric["field"]} · рядов: {len(metric["series"])}')
    editor(store, metric, metrics, revision)
    progress_panel(store)
    results_panel(store, metric)


def render(ctx):
    from atropos_ui.detector_workspace import render as render_sets
    st.title('Детекторы')
    if not isinstance(ctx.store, AccountStore):
        st.info('Войдите в рабочую учётку, чтобы настроить детекторы своих сетов.')
        return
    render_sets(ctx.store)
    with st.expander('Импорт InfluxDB и прежний расчёт'):
        legacy_render(ctx)
