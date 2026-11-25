"""Names and labels for saved queries; samples and timestamps are untouched."""
import re
from copy import deepcopy


def validate_transform(name, drop_labels, extra_labels):
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z_:][a-zA-Z0-9_:]*', name):
        raise ValueError('Имя метрики должно содержать латинские буквы, цифры, подчёркивания или двоеточия и не начинаться с цифры.')
    if not isinstance(drop_labels, (tuple, list)) or not all(isinstance(key, str) for key in drop_labels):
        raise ValueError('Укажите список удаляемых тегов.')
    if not isinstance(extra_labels, dict) or not all(isinstance(value, str) for value in extra_labels.values()):
        raise ValueError('Имена и значения дополнительных тегов должны быть строками.')
    for key in [*drop_labels, *extra_labels]:
        if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', key) or key.startswith('__'):
            raise ValueError('Имя тега должно содержать латинские буквы, цифры и подчёркивания. Имена с __ зарезервированы; имя метрики меняется отдельно.')


def transform_result(data, *, name, drop_labels=(), extra_labels=None):
    extra_labels = {} if extra_labels is None else extra_labels
    validate_transform(name, drop_labels, extra_labels)
    transformed = deepcopy(data)
    kind = transformed.get('resultType')
    if kind == 'scalar':
        transformed = {'resultType': 'vector', 'result': [{'metric': {}, 'value': transformed['result']}]}
    elif kind not in {'vector', 'matrix'}:
        raise ValueError('В каталог можно добавить только числовые временные ряды.')
    seen = set()
    for series in transformed['result']:
        labels = series['metric']
        for key in drop_labels:
            labels.pop(key, None)
        labels.update(extra_labels)
        labels['__name__'] = name
        identity = tuple(sorted(labels.items()))
        if identity in seen:
            raise ValueError('После изменения тегов несколько рядов стали одинаковыми. Оставьте отличающий тег или добавьте агрегацию в PromQL.')
        seen.add(identity)
    return transformed
