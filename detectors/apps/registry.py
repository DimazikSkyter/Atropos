"""One detector registry for validation, execution and run metadata.

Scientific implementations are imported lazily; importing configuration services
alone does not load NumPy/SciPy or a UI framework.
"""
import inspect
import hashlib
import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

DETECTOR_NAMES = frozenset({'Trend', 'CUSUM', 'MAD', 'SSA', 'ACF', 'Correlation',
                            'PredictionInterval', 'RuntimeTrend'})
RUNTIME_DEFAULTS = {'window_seconds': 120.0, 'variance_threshold': 1.0, 'min_tangent': 1.0}


@lru_cache(maxsize=1)
def detector_classes():
    from .detectors import (ACFDetector, CorrelationDetector, CusumDetector, MadDetector,
                            PredictionIntervalDetector, SSADetector, TrendDetector)
    from .runtime_trend import RuntimeTrendDetector
    return {'Trend': TrendDetector, 'CUSUM': CusumDetector, 'MAD': MadDetector,
            'SSA': SSADetector, 'ACF': ACFDetector, 'Correlation': CorrelationDetector,
            'PredictionInterval': PredictionIntervalDetector, 'RuntimeTrend': RuntimeTrendDetector}


def defaults(name):
    if name not in DETECTOR_NAMES:
        raise ValueError('Неизвестный детектор.')
    from .runtime_trend import TrendConfig
    target = TrendConfig if name == 'RuntimeTrend' else detector_classes()[name]
    result = {key: p.default for key, p in inspect.signature(target).parameters.items()
              if p.default is not inspect.Parameter.empty and not key.startswith('logger')}
    return deepcopy({**result, **RUNTIME_DEFAULTS} if name == 'RuntimeTrend' else result)


def effective_parameters(name, parameters):
    if name not in DETECTOR_NAMES or not isinstance(parameters, dict):
        raise ValueError('Неизвестный детектор или параметры не являются объектом JSON.')
    baseline = defaults(name)
    if set(parameters) - set(baseline):
        raise ValueError('Неизвестный параметр детектора.')
    try:
        json.dumps(parameters, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError('Параметры детектора должны содержать конечные значения JSON.') from None
    return deepcopy({**baseline, **parameters})


def detector(name, parameters):
    config = effective_parameters(name, parameters)
    if name == 'RuntimeTrend':
        from .runtime_trend import RuntimeTrendDetector, TrendConfig
        return RuntimeTrendDetector(TrendConfig(**config))
    return detector_classes()[name](**config)


def validate_bindings(bindings):
    if not isinstance(bindings, dict):
        raise ValueError('Привязки детекторов должны быть объектом JSON.')
    for name, parameters in bindings.items():
        detector(name, parameters)


def run_metadata(name, parameters):
    target = detector_classes()[name]
    instance = detector(name, parameters)
    config = effective_parameters(name, parameters)
    actual = instance.config if name == 'RuntimeTrend' else instance
    resolved = {key: getattr(actual, key, value) for key, value in config.items()}
    return {'schema_version': 1, 'detector': name, 'class': target.__name__,
            'implementation': 'apps.' + target.__module__.split('.')[-1] + '.' + target.__name__,
            'requested_parameters': deepcopy(parameters),
            'effective_parameters': resolved,
            'constructor_parameters': config,
            'implementation_sha256': hashlib.sha256(Path(inspect.getfile(target)).read_bytes()).hexdigest(),
            'score_kind': 'binary_detection' if name == 'RuntimeTrend' else 'bounded_severity',
            'score_range': [0, 1], 'is_probability': False}
