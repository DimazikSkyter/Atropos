"""Cycle-aware rolling ACF/PACF features, with point-based windows and lags."""

from dataclasses import dataclass
from math import ceil
from typing import Callable, Iterable

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from scipy.signal import find_peaks
from statsmodels.tsa.stattools import acf, pacf_burg

from apps.generator.config import finite_number, integer
from apps.generator.logging import logger
from apps.metrics import DataframeResult, MetricResult


CycleFunction = Callable[[np.ndarray], Iterable[int]]


def fft_periods(values: np.ndarray, *, min_period: int = 2, min_cycles: float = 2,
                min_power_fraction: float = 0.2, max_cycles: int = 3) -> tuple[int, ...]:
    """Find strong sinusoidal cycles using a Hann-windowed FFT and sine fitting.

    FFT peaks nominate candidates, then a bounded least-squares sine fit refines
    each period. Accept only cycles explaining min_power_fraction of the centered
    series variance. At least min_cycles repetitions must fit in the series.
    This is a configurable heuristic, not proof of periodicity or a p-value.
    Returned periods are rounded to the nearest integer number of points.
    """
    integer('min_period', min_period, 2)
    integer('max_cycles', max_cycles, 1)
    finite_number('min_cycles', min_cycles)
    finite_number('min_power_fraction', min_power_fraction)
    if min_cycles < 2 or not 0 < min_power_fraction <= 1:
        raise ValueError('min_cycles must be >= 2 and power fraction in (0, 1]')
    data = np.asarray(values, dtype=float)
    if data.ndim != 1 or not np.isfinite(data).all():
        raise ValueError('Cycle input must be a finite one-dimensional array')
    if len(data) < max(8, min_period * min_cycles):
        return ()
    centered = data - data.mean()
    scale = np.max(np.abs(centered))
    if scale == 0:
        return ()
    centered = centered / scale
    energy = float(centered @ centered)
    count = len(data)
    indices = np.arange(count, dtype=float)
    frequencies = np.fft.rfftfreq(8 * count)
    power = np.abs(np.fft.rfft(centered * np.hanning(count), n=8 * count)) ** 2
    peaks = list(find_peaks(power)[0])
    if power[-1] > power[-2]:
        peaks.append(len(power) - 1)
    upper_period = count / min_cycles
    candidates = [i for i in peaks if frequencies[i] > 0 and
                  min_period <= 1 / frequencies[i] <= upper_period]
    candidates.sort(key=lambda i: power[i], reverse=True)

    def unexplained(period):
        phase = 2 * np.pi * indices / period
        design = np.column_stack((np.sin(phase), np.cos(phase), np.ones(count)))
        coefficients = np.linalg.lstsq(design, centered, rcond=None)[0]
        residual = centered - design @ coefficients
        return float(residual @ residual) / energy

    periods = []
    # A bounded candidate set avoids fitting every FFT bin on large series.
    for peak in candidates[:max(12, max_cycles * 6)]:
        period = 1 / frequencies[peak]
        lower, upper = max(min_period, period * 0.85), min(upper_period, period * 1.15)
        optimized = minimize_scalar(unexplained, bounds=(lower, upper), method='bounded')
        choices = (period, lower, upper, optimized.x)
        best = min(choices, key=unexplained)
        if 1 - unexplained(best) < min_power_fraction:
            continue
        rounded = max(min_period, int(np.floor(best + 0.5)))
        if all(abs(rounded - found) > max(1, 0.03 * found) for found in periods):
            periods.append(rounded)
        if len(periods) == max_cycles:
            break
    return tuple(sorted(periods))


@dataclass(frozen=True, slots=True)
class WindowPlan:
    window: int
    step: int
    max_lag: int
    periods: tuple[int, ...]


class RollingCorrelationAnalyzer:
    """Detect cycles once over the full input, then compute every complete window.

    window = max(input_window, floor(max_period / 4), 4 * explicit_max_lag).
    step = floor(window / 2). Without explicit max_lag, use ceil(window / 4).
    The window is the actual segment length, not a second enclosing window.
    Each DataFrame row represents [start, start + window); columns are
    ('acf', lag) and ('pacf', lag), including lag zero. Incomplete tails are skipped.
    """

    def __init__(self, window: int = 20, *, max_lag: int | None = None,
                 cycle_function: CycleFunction = fft_periods):
        integer('window', window, 4)
        if max_lag is not None:
            integer('max_lag', max_lag, 1)
        if not callable(cycle_function):
            raise TypeError('cycle_function must be callable')
        self.window = int(window)
        self.max_lag = None if max_lag is None else int(max_lag)
        self.cycle_function = cycle_function

    def _plan(self, values: np.ndarray) -> WindowPlan:
        # The callback receives its own array so it cannot change analysis input.
        detected = tuple(self.cycle_function(values.copy()))
        for period in detected:
            integer('detected period', period, 2)
        periods = tuple(sorted(set(int(period) for period in detected)))
        longest = max(periods, default=0)
        window = max(self.window, longest // 4,
                     4 * self.max_lag if self.max_lag is not None else 0)
        max_lag = self.max_lag if self.max_lag is not None else ceil(window / 4)
        return WindowPlan(window, window // 2, max_lag, periods)

    def analyze(self, metric: MetricResult) -> DataframeResult:
        if not isinstance(metric, MetricResult):
            raise TypeError('metric must be a MetricResult')
        # Revalidate mutable MetricResult fields without changing the supplied object.
        snapshot = MetricResult(name=metric.name, tags=metric.tags, values=metric.values,
                                timestamps=metric.timestamps)
        if len(snapshot.timestamps) > 2 and len(set(np.diff(snapshot.timestamps))) != 1:
            raise ValueError('ACF/PACF requires uniformly spaced timestamps')
        values = np.asarray(snapshot.values, dtype=float)
        plan = self._plan(values)
        columns = pd.MultiIndex.from_product((('acf', 'pacf'), range(plan.max_lag + 1)),
                                             names=('function', 'lag'))
        rows, timestamps, constant_windows = [], [], 0
        for start in range(0, len(values) - plan.window + 1, plan.step):
            segment = values[start:start + plan.window]
            centered = segment - segment.mean()
            scale = np.max(np.abs(centered))
            if scale == 0:
                # No variance: correlations are undefined, including lag zero.
                a = p = np.full(plan.max_lag + 1, np.nan)
                constant_windows += 1
            else:
                centered = centered / scale
                a = acf(centered, nlags=plan.max_lag, fft=True, adjusted=False)
                p, _ = pacf_burg(centered, nlags=plan.max_lag, demean=False)
            rows.append(np.concatenate((a, p)))
            timestamps.append(snapshot.timestamps[start])
        frame = pd.DataFrame(rows, columns=columns, dtype=float)
        result = DataframeResult(snapshot.name, snapshot.tags, frame, timestamps, plan.window)
        # Keep the plan with the result rather than mutating shared analyzer settings.
        result.values.attrs.update({
            'step': plan.step, 'max_lag': plan.max_lag, 'periods': list(plan.periods),
            'input_window': self.window, 'constant_windows': constant_windows,
            'acf_method': 'fft', 'pacf_method': 'burg',
        })
        logger.info('Calculated rolling correlations', extra={
            'event': 'correlations_calculated', 'details': {
                'metric_name': snapshot.name, 'points': len(values), 'rows': len(rows),
                'input_window': self.window, 'window': plan.window,
                'step': plan.step, 'max_lag': plan.max_lag,
                'periods': list(plan.periods), 'constant_windows': constant_windows,
            },
        })
        return result
