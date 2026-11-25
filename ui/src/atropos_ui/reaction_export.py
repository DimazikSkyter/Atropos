"""Lossless long CSV and InfluxDB 2.x exports of the selected reaction view."""
import csv
import io
import json
import math
import re

import pandas as pd

from atropos_ui.clients import ApiError, HttpApi


def export_records(groups, components):
    for kind, series_list in (('sum', groups), ('component', components)):
        for series in series_list:
            if kind == 'sum':
                labels = series['labels']
            else:
                labels = {'sources': series['sources']}
            for index, (stamp, score) in enumerate(zip(series['times'], series['scores'])):
                yield {'kind': kind, 'series_id': series['id'], 'timestamp_ns': stamp,
                       'time': pd.Timestamp(stamp, unit='ns', tz='UTC').isoformat(), 'score': score,
                       'detector': series.get('detector', labels.get('detector', 'sum')),
                       'metric_id': series.get('metric_id', ''),
                       'source_series_id': series.get('source_series_id', ''),
                       'coverage': series['coverage'][index] if kind == 'sum' else int(score is not None),
                       'labels': json.dumps(labels, ensure_ascii=False, sort_keys=True)}


def csv_export(groups, components):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=['kind', 'series_id', 'timestamp_ns', 'time', 'score',
        'detector', 'metric_id', 'source_series_id', 'coverage', 'labels'])
    writer.writeheader()
    writer.writerows(export_records(groups, components))
    return output.getvalue().encode('utf-8-sig')


def _escape(value, characters):
    text = str(value)
    if any(ord(c) < 32 for c in text):
        raise ValueError('InfluxDB: имена и лейблы не должны содержать управляющие символы.')
    text = text.replace('\\', '\\\\')
    for character in characters:
        text = text.replace(character, '\\' + character)
    return text


def line_protocol(groups, components, measurement):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,100}', measurement):
        raise ValueError('Имя метрики: латинская буква, затем буквы, цифры или подчёркивания (до 101 символа).')
    for row in export_records(groups, components):
        score = row['score']
        if score is None:
            continue  # InfluxDB has no null numeric fields; CSV retains warmup.
        if not math.isfinite(score):
            raise ValueError('InfluxDB: score должен быть конечным числом.')
        tags = {'series_id': row['series_id'], 'detector': row['detector']}
        tags.update({k: row[k] for k in ('metric_id', 'source_series_id') if row[k]})
        # Metadata names are reserved; keep ordinary labels queryable as service=orders.
        labels = json.loads(row['labels'])
        source_tags = {}
        if row['kind'] == 'sum':
            for key, value in labels.items():
                if isinstance(value, list):
                    value = ' ↔ '.join('(отсутствует)' if v is None else v for v in value)
                if value:
                    source_tags[key] = value
        elif len(labels['sources']) == 1:
            source_tags = {k: v for k, v in labels['sources'][0]['labels'].items() if v}
        reserved = set(tags) | {'score', 'coverage', 'labels', 'time', 'metric_id', 'source_series_id'}
        for key, value in sorted(source_tags.items()):
            target = key
            while target in reserved or target.startswith('_') or (target != key and target in source_tags):
                target = 'label.' + target
            tags[target] = value
            reserved.add(target)
        encoded_tags = ','.join(_escape(k, ',= ') + '=' + _escape(v, ',= ') for k, v in sorted(tags.items()))
        # Store complete metadata as a string field too, including empty and missing labels.
        metadata = row['labels'].replace('\\', '\\\\').replace('"', '\\"')
        yield (measurement + '_' + row['kind'] + ',' + encoded_tags +
               f' score={float(score)!r},coverage={row["coverage"]}i,labels="{metadata}" {row["timestamp_ns"]}')


def write_influx(source, groups, components, measurement, *, transport=None, batch_size=5000):
    if source.kind != 'InfluxDB' or source.influx_version != '2':
        raise ValueError('Экспорт поддерживает InfluxDB 2.x.')
    if batch_size < 1:
        raise ValueError('Размер пакета должен быть положительным.')
    # Validate every line before the first write, so invalid labels cannot cause
    # an avoidable partial export. HTTP failures report already accepted batches.
    lines = list(line_protocol(groups, components, measurement))
    if not lines:
        raise ValueError('Нет рассчитанных точек для экспорта в InfluxDB.')
    api = HttpApi(source.url, datasource=source, transport=transport)
    written = 0
    for offset in range(0, len(lines), batch_size):
        batch = lines[offset:offset + batch_size]
        try:
            api.request('POST', '/api/v2/write', raw=True,
                params={'org': source.organization, 'bucket': source.bucket, 'precision': 'ns'},
                headers={'Content-Type': 'text/plain; charset=utf-8'}, content=('\n'.join(batch) + '\n').encode())
        except ApiError as exc:
            raise ApiError(f'Экспорт прерван; подтверждено точек: {written}. '
                           'Последний пакет мог быть принят частично. Повторная запись тех же точек допустима. ' + str(exc)) from exc
        written += len(batch)
    return written
