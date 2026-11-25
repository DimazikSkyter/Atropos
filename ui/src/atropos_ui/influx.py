"""Read original numeric Flux series without aggregation or a recent-time cutoff."""
import csv
import hashlib
import io
import json
import math

import pandas as pd

from atropos_ui.clients import ApiError, HttpApi
from atropos_ui.resource_limits import limit

META = {'', 'result', 'table', '_start', '_stop', '_time', '_value', '_field', '_measurement'}


def metric_id(source_id, measurement, field):
    return hashlib.sha256(json.dumps([source_id, measurement, field]).encode()).hexdigest()[:24]


def parse_flux(raw, source_id, *, max_points=None, max_series=None):
    max_points = max_points if max_points is not None else limit('IMPORT_MAX_POINTS', 500_000)
    max_series = max_series if max_series is not None else limit('IMPORT_MAX_SERIES', 10_000)
    point_count = series_count = 0
    header = None
    grouped, skipped = {}, 0
    for fields in csv.reader(io.StringIO(raw)):
        if not fields or fields[0].startswith('#'):
            continue
        if '_time' in fields and '_value' in fields:
            header = fields
            continue
        if header is None or len(header) != len(fields):
            raise ApiError('Некорректный CSV от InfluxDB.')
        row = dict(zip(header, fields))
        try:
            value = float(row['_value'])
            if not math.isfinite(value):
                raise ValueError
        except (TypeError, ValueError):
            skipped += 1
            continue
        try:
            timestamp = pd.Timestamp(row['_time'])
            if pd.isna(timestamp):
                raise ValueError('missing timestamp')
            stamp = int(timestamp.value)
            measurement, field = row['_measurement'], row['_field']
            if not measurement or not field:
                raise ValueError
        except (ValueError, KeyError, OverflowError) as exc:
            raise ApiError('Некорректное время или имя метрики в InfluxDB.') from exc
        uid = metric_id(source_id, measurement, field)
        metric = grouped.setdefault(uid, {'id': uid, 'datasource_id': source_id,
            'measurement': measurement, 'field': field, 'name': measurement + ' / ' + field, 'series': {}})
        labels = {k: v for k, v in row.items() if k not in META}
        key = json.dumps(labels, sort_keys=True, ensure_ascii=False)
        if key not in metric['series']:
            series_count += 1
        point_count += 1
        if point_count > max_points or series_count > max_series:
            raise ApiError('История превышает лимит ATROPOS_IMPORT_MAX_POINTS / ATROPOS_IMPORT_MAX_SERIES. '
                           'Сузьте источник или настройте лимиты; неполный импорт не сохранён.')
        series = metric['series'].setdefault(key, {'labels': labels, 'points': {}})
        if stamp in series['points']:
            raise ApiError('В ответе InfluxDB есть повторный timestamp для одного ряда.')
        series['points'][stamp] = value
    for metric in grouped.values():
        metric['series'] = [{'labels': s['labels'], 'times': sorted(s['points']),
                             'values': [s['points'][t] for t in sorted(s['points'])]}
                            for _, s in sorted(metric['series'].items())]
    return grouped, skipped


def read_source(source, *, transport=None):
    if source.kind != 'InfluxDB' or source.influx_version != '2':
        raise ValueError('Импорт истории поддерживает InfluxDB 2.x / Flux.')
    query = f'from(bucket: {json.dumps(source.bucket)}) |> range(start: 0) |> sort(columns: ["_time"])'
    raw = HttpApi(source.url, datasource=source, transport=transport).request(
        'POST', '/api/v2/query', raw=True, params={'org': source.organization},
        headers={'Content-Type': 'application/vnd.flux', 'Accept': 'text/csv'}, content=query.encode())
    return parse_flux(raw, source.id)


def read_window(source, start_ns, end_ns, metrics, *, transport=None):
    """Fetch only the registered fields in this half-open runtime window."""
    if source.kind != 'InfluxDB' or source.influx_version != '2':
        raise ValueError('Рантайм импортированных метрик поддерживает InfluxDB 2.x / Flux.')
    if end_ns <= start_ns:
        raise ValueError('Конец окна должен быть позже начала.')
    if not metrics:
        return {}, 0
    pairs = sorted({(m['measurement'], m['field']) for m in metrics})
    predicate = ' or '.join(f'(r._measurement == {json.dumps(m)} and r._field == {json.dumps(f)})' for m, f in pairs)
    first, last = (pd.Timestamp(t, unit='ns', tz='UTC').isoformat() for t in (start_ns, end_ns))
    query = (f'from(bucket: {json.dumps(source.bucket)}) '
             f'|> range(start: time(v: {json.dumps(first)}), stop: time(v: {json.dumps(last)})) '
             f'|> filter(fn: (r) => {predicate}) |> sort(columns: ["_time"])')
    raw = HttpApi(source.url, datasource=source, transport=transport).request(
        'POST', '/api/v2/query', raw=True, params={'org': source.organization},
        headers={'Content-Type': 'application/vnd.flux', 'Accept': 'text/csv'}, content=query.encode())
    return parse_flux(raw, source.id)


def descriptors(data, detectors):
    return [{**{k: v for k, v in metric.items() if k != 'series'},
             'series': [{'labels': s['labels'], 'count': len(s['times']),
                         'start': s['times'][0], 'end': s['times'][-1]} for s in metric['series']],
             'detectors': {name: {} for name in detectors}, 'extra_labels': {}, 'drop_labels': [], 'peers': []}
            for metric in data.values()]
