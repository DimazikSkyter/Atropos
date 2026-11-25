"""Detector reaction selection and sums, independent of UI and storage clients."""
import hashlib
import json
import math
import re
from collections import defaultdict


def stable_id(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def source_reference(metric_id, series, name='', *, metric=None):
    """Identity uses original tags, so renaming or transforming tags preserves it."""
    labels = series.get('effective_labels')
    if labels is None:
        labels = json.loads(series['label'])
    original = series.get('labels', labels)
    reference = {'metric_id': metric_id, 'source_series_id': stable_id([metric_id, original]),
                 'name': name, 'labels': labels, 'source_labels': original}
    reference.update({k: metric[k] for k in ('datasource_id', 'measurement', 'field') if metric and k in metric})
    return reference


def collect_components(result):
    """Read current and legacy replays; count identical correlation pairs once."""
    references, display_index = {}, defaultdict(list)
    for uid, metric in result['metrics'].items():
        for index, series in enumerate(metric['series']):
            reference = source_reference(uid, series, metric['name'], metric=metric)
            references[uid, index] = reference
            display_index[metric['name'] + ' ' + series['label']].append(reference)
    components, warnings = {}, list(result.get('warnings', []))
    for uid, metric in result['metrics'].items():
        if metric.get('status') == 'error':
            warnings.append(metric['name'] + ': ' + metric['message'])
            continue
        for binding_id, output in metric['detectors'].items():
            detector = output.get('detector', binding_id)
            if output.get('message'):
                warnings.append(metric['name'] + ' · ' + detector + ': ' + output['message'])
            for index, series in enumerate(output.get('series', [])):
                sources = series.get('sources')
                if sources is None and detector != 'Correlation':
                    sources = [references[uid, index]]
                if sources is None:
                    # Old persisted pairs had only display names. Resolve uniquely;
                    # ambiguous names must never silently point to another series.
                    pairs = []
                    for label, left in display_index.items():
                        prefix = label + ' ↔ '
                        if series['label'].startswith(prefix):
                            right = display_index.get(series['label'][len(prefix):], [])
                            if len(left) == len(right) == 1:
                                pairs.append([left[0], right[0]])
                    if len(pairs) != 1:
                        warnings.append('Correlation: пересчитайте результаты для определения исходных рядов.')
                        continue
                    sources = pairs[0]
                sources = sorted(sources, key=lambda s: s['source_series_id'])
                key = [detector, [s['source_series_id'] for s in sources], output.get('parameters', {})]
                if binding_id != detector:
                    key.append(binding_id)
                identity = stable_id(key)
                times = series.get('times')
                if times is None:
                    times = metric['series'][index]['times']
                scores = series['scores']
                if len(times) != len(scores) or len(set(times)) != len(times):
                    raise ValueError('Некорректная временная сетка реакции детектора.')
                if any(v is not None and (not math.isfinite(v) or v < 0) for v in scores):
                    raise ValueError('Реакции должны быть конечными неотрицательными score или пропусками.')
                components.setdefault(identity, {'id': identity, 'detector': detector,
                    'owner_metric_id': uid,
                    'instance_id': binding_id, 'name': output.get('name', metric['name'] + '_' + detector),
                    'parameters': output.get('parameters', {}),
                    'score_kind': output.get('metadata', {}).get('score_kind', 'legacy_unspecified'),
                    'metric_id': ' ↔ '.join(s['metric_id'] for s in sources),
                    'source_series_id': ' ↔ '.join(s['source_series_id'] for s in sources),
                    'sources': sources, 'times': times, 'scores': scores})
    return list(components.values()), list(dict.fromkeys(warnings))


def select_components(components, *, labels=None, label_regex=None, detectors=None):
    labels = {} if labels is None else labels
    label_regex = {} if label_regex is None else label_regex
    for mapping in (labels, label_regex):
        if not isinstance(mapping, dict) or not all(isinstance(k, str) and k and isinstance(v, str)
                                                   for k, v in mapping.items()):
            raise ValueError('Фильтры лейблов должны быть объектами JSON со строковыми ключами и значениями.')
    try:
        patterns = {k: re.compile(v) for k, v in label_regex.items()}
    except re.error as exc:
        raise ValueError('Некорректное регулярное выражение: ' + str(exc)) from None

    def matches(source):
        tags = source['labels']
        return (all(k in tags and tags[k] == v for k, v in labels.items())
                and all(k in tags and p.fullmatch(tags[k]) is not None for k, p in patterns.items()))

    return [c for c in components if (detectors is None or c['detector'] in detectors)
            and all(matches(s) for s in c['sources'])]


def group_labels(component, group_by):
    # Keep mixed-label pairs explicit instead of attributing a reaction twice.
    return {key: (component['detector'] if key == 'detector' else
                  tuple(sorted({s['labels'].get(key) for s in component['sources']},
                               key=lambda value: (value is not None, value or ''))))
            for key in group_by}


def group_name(labels):
    def value_text(value):
        if isinstance(value, tuple):
            return ' ↔ '.join('(отсутствует)' if v is None else v for v in value)
        return value
    return ', '.join(key + '=' + value_text(value) for key, value in labels.items()) or 'Все реакции'


def aggregate_components(components, group_by):
    grouped = {}
    for component in components:
        labels = group_labels(component, group_by)
        key = stable_id(labels)
        group = grouped.setdefault(key, {'id': key, 'labels': labels, 'components': [], 'points': defaultdict(list)})
        group['components'].append(component['id'])
        for stamp, value in zip(component['times'], component['scores']):
            # Record warmup timestamps too, retaining gaps rather than fake zeros.
            values = group['points'][stamp]
            if value is not None:
                values.append(value)
    groups = []
    for group in grouped.values():
        stamps = sorted(group.pop('points').items())
        group.update(name=group_name(group['labels']), times=[t for t, _ in stamps],
                     scores=[math.fsum(v) if v else None for _, v in stamps],
                     coverage=[len(v) for _, v in stamps])
        # Exporting another detector/filter selection must not overwrite this sum.
        group['id'] = stable_id([group['labels'], sorted(group['components'])])
        groups.append(group)
    return sorted(groups, key=lambda g: g['name'])
