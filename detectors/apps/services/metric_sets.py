"""Account-owned runtime sets: explicit metric × detector bindings."""
import copy
import hashlib
import json
import math
import re

from ..registry import DETECTOR_NAMES, validate_bindings

RELATIVE = re.compile(r'now(?:[+-]\d+[smhdwMy])*(?:/[smhdwMy])?')


def definition_id(name):
    return 'promql_' + hashlib.sha256(name.encode()).hexdigest()[:24]


def catalog_metrics(workspace):
    metrics = copy.deepcopy(workspace.get('metrics', []))
    for item in workspace.get('definitions', []):
        metrics.append({**copy.deepcopy(item), 'id': definition_id(item['name']), 'kind': 'promql',
                        'measurement': item['name'], 'field': 'value', 'series': [], 'detectors': {}, 'peers': []})
    return metrics


def detector_instances(bindings):
    if not isinstance(bindings, dict):
        raise ValueError('Некорректные привязки детекторов.')
    result = []
    for uid, config in bindings.items():
        if not isinstance(uid, str) or not uid or len(uid) > 160 or not isinstance(config, dict):
            raise ValueError('Некорректный экземпляр детектора.')
        if uid in DETECTOR_NAMES and 'kind' not in config:
            item = {'id': uid, 'kind': uid, 'name': uid, 'parameters': config}
        else:
            if set(config) != {'kind', 'name', 'parameters'}:
                raise ValueError('Экземпляр детектора должен содержать kind, name и parameters.')
            item = {'id': uid, **config}
        if not isinstance(item['name'], str) or not re.fullmatch(r'[a-zA-Z_:][a-zA-Z0-9_:]*', item['name']):
            raise ValueError('Укажите имя реакции из латинских букв, цифр и подчёркиваний.')
        if not isinstance(item['kind'], str) or item['kind'] not in DETECTOR_NAMES:
            raise ValueError('Неизвестный детектор.')
        validate_bindings({item['kind']: item['parameters']})
        result.append(copy.deepcopy(item))
    return result


def normalize_time(expression):
    if not isinstance(expression, str):
        raise ValueError('Укажите относительное время, например now() - 5m.')
    value = re.sub(r'\s+', '', expression).replace('now()', 'now')
    if not RELATIVE.fullmatch(value):
        raise ValueError('Укажите относительное время, например now() - 5m или now().')
    return value


def validate_metric_set(item, metrics):
    if not isinstance(item, dict) or set(item) != {'id', 'name', 'start', 'end', 'refresh_seconds', 'members'}:
        raise ValueError('Некорректная конфигурация сета.')
    if any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 160 for k in ('id', 'name')):
        raise ValueError('Укажите ID и имя сета длиной до 160 символов.')
    interval = item['refresh_seconds']
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 1:
        raise ValueError('Интервал обновления должен быть не меньше 1 секунды.')
    if not isinstance(item['members'], list) or not item['members']:
        raise ValueError('Добавьте в сет хотя бы одну метрику.')
    known, seen, reaction_names = {m['id'] for m in metrics}, set(), set()
    for member in item['members']:
        if not isinstance(member, dict) or set(member) != {'metric_id', 'detectors', 'peers'}:
            raise ValueError('Участник сета должен содержать metric_id, detectors и peers.')
        uid = member['metric_id']
        if not isinstance(uid, str) or uid not in known:
            raise ValueError('Метрика сета не принадлежит этой учётке.')
        if uid in seen:
            raise ValueError('Объедините детекторы одной метрики в одном участнике сета.')
        seen.add(uid)
        bindings = member['detectors']
        for instance in detector_instances(bindings):
            if instance['id'] not in DETECTOR_NAMES:
                if instance['name'] in reaction_names or instance['name'] in {m['name'] for m in metrics if 'name' in m}:
                    raise ValueError('Имена реакций в сете должны быть уникальны и отличаться от имён метрик.')
                reaction_names.add(instance['name'])
        peers = member['peers']
        if (not isinstance(peers, list) or any(not isinstance(p, str) or p not in known or p == uid for p in peers)
                or len(set(peers)) != len(peers)):
            raise ValueError('Связанные метрики сета не принадлежат этой учётке или повторяются.')
    result = copy.deepcopy(item)
    result.update(name=item['name'].strip(), start=normalize_time(item['start']), end=normalize_time(item['end']))
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError):
        raise ValueError('Параметры сета должны содержать конечные значения JSON.') from None
    return result
