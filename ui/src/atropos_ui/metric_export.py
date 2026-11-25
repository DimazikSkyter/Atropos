"""Metrics and named detector reactions share one queryable series/export format."""
import csv
import io
import json
import math
from decimal import Decimal

import pandas as pd
import streamlit as st

from atropos_ui.clients import ApiError, HttpApi
from atropos_ui.reaction_export import _escape
from atropos_ui.service_api import collect_components


def result_entities(result):
    entities = []
    for uid, metric in result['metrics'].items():
        for series in metric['series']:
            entities.append({'key': 'm:' + uid, 'name': metric['name'], 'kind': 'metric',
                             'labels': json.loads(series['label']), 'times': series['times'], 'values': series['values']})
    components, _ = collect_components(result)
    for component in components:
        labels = dict(component['sources'][0]['labels']) if len(component['sources']) == 1 else {
            'sources': json.dumps(component['sources'], ensure_ascii=False, sort_keys=True)}
        for key, value in {'detector': component['detector'], 'detector_instance': component['instance_id']}.items():
            if key in labels:
                original_key = 'source_' + key
                while original_key in labels:
                    original_key = 'source_' + original_key
                labels[original_key] = labels.pop(key)
            labels[key] = value
        entities.append({'key': 'r:' + component['owner_metric_id'] + ':' + component['instance_id'],
                         'name': component['name'], 'kind': 'reaction', 'labels': labels,
                         'source_keys': ['m:' + s['metric_id'] for s in component['sources']],
                         'times': component['times'], 'values': component['scores']})
    return entities


def query_entities(data, name):
    kind = data['resultType']
    rows = data['result'] if kind in {'matrix', 'vector'} else [{'metric': {}, 'value': data['result']}]
    entities = []
    for row in rows:
        samples = row.get('values', [row.get('value')])
        values = [float(v) if kind != 'string' else float('nan') for _, v in samples]
        labels = dict(row['metric'])
        metric_name = labels.pop('__name__', name)
        entities.append({'name': metric_name, 'kind': 'metric', 'labels': labels,
                         'times': [int(Decimal(str(t)) * 1_000_000_000) for t, _ in samples],
                         'values': [v if math.isfinite(v) else None for v in values]})
    return entities


def metric_csv(entities):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['name', 'timestamp_ns', 'time', 'value', 'labels'])
    for entity in entities:
        for stamp, value in zip(entity['times'], entity['values']):
            writer.writerow([entity['name'], stamp, pd.Timestamp(stamp, unit='ns', tz='UTC').isoformat(),
                             value, json.dumps(entity['labels'], ensure_ascii=False, sort_keys=True)])
    return output.getvalue().encode('utf-8-sig')


def metric_json(entities):
    return json.dumps({'status': 'success', 'data': {'resultType': 'matrix', 'result': [
        {'metric': {**e['labels'], '__name__': e['name']},
         'values': [[str(Decimal(t) / 1_000_000_000), str(v) if v is not None else 'NaN']
                    for t, v in zip(e['times'], e['values'])]} for e in entities]}}, ensure_ascii=False).encode()


def metric_lines(entities, prefix=''):
    seen = set()
    for entity in entities:
        measurement = _escape(prefix + entity['name'], ', ')
        if not measurement:
            raise ValueError('Укажите непустое имя метрики.')
        tags = ','.join(_escape(k, ',= ') + '=' + _escape(v, ',= ')
                        for k, v in sorted(entity['labels'].items()) if k and v != '')
        for stamp, value in zip(entity['times'], entity['values']):
            if value is None:
                continue
            if not math.isfinite(value):
                raise ValueError('Экспортируемое значение должно быть конечным числом.')
            identity = measurement, tags, stamp
            if identity in seen:
                raise ValueError('Экспорт содержит одинаковые имена, теги и timestamps. Переименуйте метрики перед записью.')
            seen.add(identity)
            yield measurement + (',' + tags if tags else '') + f' value={float(value)!r} {stamp}'


def write_metric_influx(source, entities, prefix='', *, transport=None, batch_size=5000):
    if source.kind != 'InfluxDB' or source.influx_version != '2':
        raise ValueError('Для экспорта выберите InfluxDB 2.x.')
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError('Размер пакета должен быть положительным целым числом.')
    lines = list(metric_lines(entities, prefix))
    if not lines:
        raise ValueError('Нет числовых точек для записи. Детекторы могут находиться в прогреве.')
    api, written = HttpApi(source.url, datasource=source, transport=transport), 0
    for offset in range(0, len(lines), batch_size):
        batch = lines[offset:offset + batch_size]
        try:
            api.request('POST', '/api/v2/write', raw=True,
                        params={'org': source.organization, 'bucket': source.bucket, 'precision': 'ns'},
                        headers={'Content-Type': 'text/plain; charset=utf-8'}, content=('\n'.join(batch) + '\n').encode())
        except ApiError as exc:
            raise ApiError(f'Запись прервана. Подтверждено точек: {written}; последний пакет мог быть принят частично. '
                           'Повторная запись тех же точек допустима. ' + str(exc)) from exc
        written += len(batch)
    return written


def export_panel(store, entities, *, key, revision=None, expanded=False):
    with st.expander('Экспорт метрик и реакций', expanded=expanded):
        if not entities:
            st.info('Сначала загрузите данные за выбранный период.')
            return
        left, right = st.columns(2)
        left.download_button('Скачать CSV', metric_csv(entities), 'atropos-metrics.csv', 'text/csv', key=key + '_csv')
        right.download_button('Скачать JSON', metric_json(entities), 'atropos-metrics.json',
                              'application/json', key=key + '_json')
        sources = [s for s in store.sources() if s.kind == 'InfluxDB' and s.influx_version == '2']
        if not sources:
            st.caption('Для записи в InfluxDB добавьте подключение InfluxDB 2.x в своей учётке.')
            return
        source = st.selectbox('Куда экспортировать', sources, format_func=lambda s: s.name, key=key + '_target')
        prefix = st.text_input('Префикс имён при экспорте', key=key + '_prefix')
        st.caption('Каждая метрика и реакция записывается под своим именем, числовое поле — value. '
                   'Пропуски прогрева сохраняются в файлах и пропускаются при записи в InfluxDB.')
        if st.button('Экспортировать в InfluxDB', key=key + '_write'):
            try:
                if revision is not None and store.snapshot()[1] != revision:
                    raise ValueError('Настройки изменились. Обновите расчёт перед экспортом.')
                count = write_metric_influx(source, entities, prefix)
                st.success(f'Записано точек: {count}.')
            except (ApiError, ValueError) as exc:
                st.error(str(exc))
