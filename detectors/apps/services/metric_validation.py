"""Account metric invariants enforced at the shared storage boundary."""
from copy import deepcopy

from ..registry import validate_bindings


def validate_metric(metric, known_ids):
    result = deepcopy(metric)
    if not isinstance(result.get('name'), str) or not result['name'].strip():
        raise ValueError('Укажите непустое имя метрики.')
    result['name'] = result['name'].strip()
    extra, dropped = result.get('extra_labels', {}), result.get('drop_labels', [])
    if not isinstance(extra, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str)
                                          for k, v in extra.items()):
        raise ValueError('Лейблы должны содержать строковые имена и значения.')
    if (not isinstance(dropped, list) or any(not isinstance(k, str) or not k for k in dropped)
            or len(set(dropped)) != len(dropped)):
        raise ValueError('Удаляемые лейблы должны быть списком уникальных непустых строк.')
    validate_bindings(result.get('detectors', {}))
    peers = result.get('peers', [])
    if (not isinstance(peers, list) or any(not isinstance(p, str) or p not in known_ids or p == result['id']
                                         for p in peers) or len(set(peers)) != len(peers)):
        raise ValueError('Связанные метрики не принадлежат этой учётке или повторяются.')
    return result
