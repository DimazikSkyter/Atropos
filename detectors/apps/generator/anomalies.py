"""Apply an arbitrary value transformation over point indices, preserving input."""

from numbers import Real
from typing import Callable

from apps.metrics import MetricResult

from .config import finite_number, integer
from .logging import logger


class AnomalyGenerator:
    """Snapshot a MetricResult and transform [start, stop), using original values.

    Neither the supplied metric nor its tags/timestamps/values are mutated.
    Calling generate() again applies the function to the same original snapshot.
    """

    def __init__(self, metric: MetricResult, start: int, stop: int,
                 anomaly: Callable[[float | int], Real]):
        if not isinstance(metric, MetricResult):
            raise TypeError('metric must be a MetricResult')
        integer('start', start, 0)
        integer('stop', stop, 1)
        if not start < stop <= len(metric.values):
            raise ValueError('Range must satisfy 0 <= start < stop <= number of points')
        if not callable(anomaly):
            raise TypeError('anomaly must be callable')
        self._original = MetricResult(name=metric.name, tags=metric.tags,
                                      values=metric.values, timestamps=metric.timestamps)
        self.start, self.stop = int(start), int(stop)
        self.anomaly = anomaly

    def generate(self) -> MetricResult:
        values = list(self._original.values)
        for i in range(self.start, self.stop):
            try:
                transformed = self.anomaly(self._original.values[i])
                finite_number('anomaly result', transformed)
                values[i] = float(transformed)
            except Exception:
                logger.exception('Anomaly transformation failed', extra={
                    'event': 'anomaly_failed', 'details': {
                        'metric_name': self._original.name, 'point_index': i,
                        'start': self.start, 'stop': self.stop,
                    },
                })
                raise
        result = MetricResult(name=self._original.name, tags=self._original.tags,
                              values=values, timestamps=self._original.timestamps)
        logger.info('Applied anomaly', extra={
            'event': 'anomaly_applied', 'details': {
                'metric_name': result.name, 'start': self.start, 'stop': self.stop,
                'affected_points': self.stop - self.start,
            },
        })
        return result
