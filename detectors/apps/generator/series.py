"""NumPy generation around a constant expected baseline, with optional cycles."""

from dataclasses import asdict
from datetime import datetime
from typing import Mapping, Sequence

import numpy as np

from apps.metrics import MetricResult

from .config import CycleConfig, NoiseConfig, SpikeConfig, finite_number, integer
from .logging import logger


class TimeSeriesGenerator:
    """Time is fixed at construction; generate() only takes a point count.

    Without start_time, use today's local midnight. A naive datetime uses the
    local timezone; an aware datetime or integer Unix seconds is unambiguous.
    Each call starts at start_time and consumes the instance's random stream.
    Fresh instances with the same seed and arguments reproduce the same series.
    """

    def __init__(self, *, mean: float = 100.0, name: str = 'synthetic_metric',
                 tags: Mapping[str, str] | None = None,
                 noise: NoiseConfig | None = None,
                 cycles: Sequence[CycleConfig] = (),
                 spikes: SpikeConfig | None = None,
                 start_time: datetime | int | None = None,
                 step_seconds: int = 15, seed: int | None = None):
        finite_number('mean', mean)
        integer('step_seconds', step_seconds, 1)
        if seed is not None:
            integer('seed', seed, 0)
        self.noise = NoiseConfig() if noise is None else noise
        self.spikes = SpikeConfig() if spikes is None else spikes
        if not isinstance(self.noise, NoiseConfig) or not isinstance(self.spikes, SpikeConfig):
            raise TypeError('noise and spikes must be NoiseConfig and SpikeConfig')
        self.cycles = tuple(cycles)
        if len(self.cycles) > 3 or any(not isinstance(c, CycleConfig) for c in self.cycles):
            raise ValueError('cycles must contain at most three CycleConfig objects')
        if start_time is None:
            start_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if isinstance(start_time, datetime):
            stamp = start_time.timestamp()
            if not stamp.is_integer():
                raise ValueError('start_time must have whole-second precision')
            start_time = int(stamp)
        if isinstance(start_time, bool) or not isinstance(start_time, (int, np.integer)):
            raise ValueError('start_time must be a datetime or integer Unix seconds')
        # Reuse the MetricResult validation for metadata without retaining caller containers.
        metadata = MetricResult(name=name, tags=tags)
        self.mean = float(mean)
        self.name = metadata.name
        self.tags = metadata.tags
        self.start_time = int(start_time)
        self.step_seconds = int(step_seconds)
        self.seed = None if seed is None else int(seed)
        self._rng = np.random.default_rng(self.seed)
        self.background_anomaly_ranges: tuple[tuple[int, int], ...] = ()

    def _noise(self, points: int) -> np.ndarray:
        cfg, rng = self.noise, self._rng
        noise = np.zeros(points, dtype=float)
        if cfg.white_std_percent:
            noise += cfg.white_std_percent * rng.normal(size=points)
        if cfg.correlated_std_percent:
            correlated = np.empty(points, dtype=float)
            # Draw from the stationary distribution, avoiding an initial warm-up ramp.
            correlated[0] = rng.normal()
            innovations = rng.normal(size=points - 1) * np.sqrt(1 - cfg.correlation ** 2)
            for i, innovation in enumerate(innovations, 1):
                correlated[i] = cfg.correlation * correlated[i - 1] + innovation
            noise += cfg.correlated_std_percent * correlated
        if cfg.heavy_tail_std_percent:
            normalized = rng.standard_t(cfg.degrees_of_freedom, size=points)
            normalized *= np.sqrt((cfg.degrees_of_freedom - 2) / cfg.degrees_of_freedom)
            noise += cfg.heavy_tail_std_percent * normalized
        return noise * (abs(self.mean) / 100)

    def _spikes(self, values: np.ndarray) -> tuple[tuple[int, int], ...]:
        cfg, rng = self.spikes, self._rng
        ranges = []
        if cfg.probability == 0 or cfg.max_amplitude_percent == 0 or self.mean == 0:
            return ()
        i = 0
        while i < len(values):
            if rng.random() < cfg.probability:
                stop = min(len(values), i + int(rng.integers(1, cfg.max_points + 1)))
                amplitudes = rng.uniform(cfg.min_amplitude_percent,
                                         cfg.max_amplitude_percent, size=stop - i)
                values[i:stop] += amplitudes * (abs(self.mean) / 100)
                ranges.append((i, stop))
                i = stop + 1
            else:
                i += 1
        return tuple(ranges)

    def generate(self, points: int = 1000) -> MetricResult:
        integer('points', points, 1)
        points = int(points)
        indices = np.arange(points, dtype=float)
        values = self.mean + self._noise(points)
        for cycle in self.cycles:
            values += (abs(self.mean) / 100) * cycle.amplitude_percent * np.sin(
                2 * np.pi * indices / cycle.period_points + cycle.phase_radians)
        ranges = self._spikes(values)
        result = MetricResult(name=self.name, tags=self.tags, values=values.tolist(),
                              timestamps=[self.start_time + i * self.step_seconds
                                          for i in range(points)])
        self.background_anomaly_ranges = ranges
        logger.info('Generated synthetic metric', extra={
            'event': 'series_generated', 'details': {
                'metric_name': self.name, 'points': points, 'mean': self.mean,
                'start_timestamp': self.start_time, 'step_seconds': self.step_seconds,
                'seed': self.seed, 'noise': asdict(self.noise),
                'cycles': [asdict(c) for c in self.cycles],
                'spikes': asdict(self.spikes), 'background_events': len(ranges),
            },
        })
        return result
