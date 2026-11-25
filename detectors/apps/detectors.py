"""Detectors, including a noise-relative CUSUM for bursts and reversals."""

from collections import deque
from dataclasses import dataclass, replace
import logging
import math
from numbers import Integral, Real
from statistics import mean, pstdev, stdev
from typing import Iterable

import numpy as np
from scipy.linalg import eigh
from scipy.signal import find_peaks
from scipy.stats import t as student_t


class Detector:
    def __init__(self, logger_name: str | None = None,
                 logger_props: dict[str, str] | None = None):
        if logger_name is None:
            logger_name = self.__class__.__name__
        self.logger = logging.getLogger(logger_name)
        self.logger.setLevel((logger_props or {}).get('level', 'INFO'))


@dataclass(frozen=True, slots=True)
class TrendResult:
    """Causal change severity, not a probability; indices are zero-based.

    horizontal is unknown (None) before warmup. change_start is a retrospective
    estimate issued at confirmed_at, never a decision available at change_start.
    direction describes the CHANGE of slope (a slowdown is -1), not its sign.
    """

    score: float = 0.0
    horizontal: bool | None = None
    ready: bool = False
    slope: float = 0.0
    reference_slope: float = 0.0
    angle: float = 0.0
    direction: int = 0
    change_start: int | None = None
    confirmed_at: int | None = None
    detected: bool = False
    evidence: float = 0.0


class _TrendWindow:
    """OLS on local integer x; periodic rebasing bounds roundoff drift."""

    def __init__(self, size):
        self.size = size
        self.values = deque()
        self.sy = self.syy = self.sxy = 0.0
        self.ticks = 0
        self.origin = 0.0

    def append(self, value):
        expired = None
        if len(self.values) == self.size:
            expired = self.values.popleft()
            local_expired = expired - self.origin
            self.sy -= local_expired
            self.syy -= local_expired * local_expired
            self.sxy -= self.sy
        local = value - self.origin
        self.sxy += len(self.values) * local
        self.sy += local
        self.syy += local * local
        self.values.append(value)
        self.ticks += 1
        if self.ticks >= self.size:
            self.origin = self.values[0]
            local_values = [v - self.origin for v in self.values]
            self.sy = math.fsum(local_values)
            self.syy = math.fsum(v * v for v in local_values)
            self.sxy = math.fsum(i * v for i, v in enumerate(local_values))
            self.ticks = 0
        return expired

    def fit(self):
        n = len(self.values)
        center = (n - 1) * 0.5
        sxx = n * (n * n - 1) / 12.0
        cross = self.sxy - center * self.sy
        slope = cross / sxx
        sse = max(0.0, self.syy - self.sy * self.sy / n - slope * cross)
        return self.origin + self.sy / n, slope, math.sqrt(sse / max(1, n - 2))

    def uncertainty(self, projection=0.0):
        """HAC errors of the fitted slope and projected mean (Bartlett weights).

        Correlated residuals are not counted as independent evidence. Unlike
        single-point residual spread, these errors describe the fitted line.
        Only values already admitted to this rolling window are used.
        """
        n = len(self.values)
        center, slope, _ = self.fit()
        x = np.arange(n, dtype=float) - (n - 1) * .5
        sxx = n * (n * n - 1) / 12.0
        residual = np.fromiter(self.values, dtype=float, count=n) - center - slope * x
        influences = np.column_stack((residual * x / sxx,
                                       residual * (1.0 / n + projection * x / sxx)))
        variance = np.sum(influences * influences, axis=0)
        bandwidth = max(1, min(n // 4, int(math.sqrt(n))))
        for lag in range(1, bandwidth + 1):
            variance += (2 * (1 - lag / (bandwidth + 1))
                         * np.sum(influences[lag:] * influences[:-lag], axis=0))
        errors = np.sqrt(np.maximum(0.0, variance) * n / max(1, n - 2))
        return float(errors[0]), float(errors[1])


class TrendDetector(Detector):
    """Two disjoint rolling windows and a finite directional evidence memory.

    data_max_size includes the terminal window. The preceding samples estimate
    a line and residual scale sqrt(SSE/(n-2)); the tail estimates displacement
    and slope. Requiring BOTH suppresses offset-only changes. Regression mode
    compares these changes to the HAC uncertainty of the fitted lines, allowing
    a persistent growth to stand out despite large isolated residuals. The
    residual mode retains the original per-point normalization for comparison.

    Once enough evidence exists, a linear-time two-segment fit localizes the
    breakpoint and rejects level steps and isolated outliers. This scan runs
    only for a new candidate, at most once per inertia_window observations.
    The active event is released after evidence disappears or reverses.
    Baseline adaptation eventually clears the score on an established regime.

    Regression uncertainty costs O(data_max_size * sqrt(data_max_size)) per
    ready point with bounded window arrays; residual mode retains O(1) moments.
    Breakpoint scans cost O(data_max_size) at most once per inertia_window.
    Memory is O(data_max_size + inertia_window); there is no history growth.

    Samples MUST be equally spaced. slope = (value/value_scale) per time_scale;
    angle = degrees(atan(slope)). Defaults use one value unit per sample.
    This explicit axis convention is necessary for a meaningful degree limit.
    One instance per series; use ready to distinguish warmup from measured zero.
    """

    def __init__(self, window: int = 40, data_max_size: int = 240, *,
                 inertia_window: int | None = None, threshold: float = 2.0,
                 value_scale: float = 1.0, sample_interval: float = 1.0,
                 time_scale: float = 1.0, angle_tolerance: float = 1.0,
                 noise_floor: float = 1e-9,
                 evidence_mode: str = 'regression',
                 logger_name: str | None = None,
                 logger_props: dict[str, str] | None = None):
        if inertia_window is None:
            inertia_window = max(3, int(window) // 3) if isinstance(window, Integral) else 3
        for name, value, minimum in (('window', window, 6),
                                     ('data_max_size', data_max_size, 18),
                                     ('inertia_window', inertia_window, 2)):
            if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
                raise ValueError(f'{name} must be an integer >= {minimum}')
        if data_max_size < 3 * window:
            raise ValueError('data_max_size must be >= 3 * window')
        if inertia_window > window:
            raise ValueError('inertia_window must be <= window')
        for name, value in (('threshold', threshold), ('value_scale', value_scale),
                            ('sample_interval', sample_interval), ('time_scale', time_scale),
                            ('noise_floor', noise_floor), ('angle_tolerance', angle_tolerance)):
            self._number(name, value)
            if value <= 0:
                raise ValueError(f'{name} must be positive')
        if angle_tolerance >= 90:
            raise ValueError('angle_tolerance must be < 90')
        if evidence_mode not in ('regression', 'residual'):
            raise ValueError('evidence_mode must be regression or residual')
        super().__init__(logger_name, logger_props)
        self.evidence_mode = evidence_mode
        self.window, self.data_max_size = int(window), int(data_max_size)
        self.inertia_window, self.threshold = int(inertia_window), float(threshold)
        self.value_scale, self.sample_interval = float(value_scale), float(sample_interval)
        self.time_scale, self.angle_tolerance = float(time_scale), float(angle_tolerance)
        self.noise_floor = float(noise_floor)
        self._slope_scale = self.time_scale / self.sample_interval
        self._horizontal_limit = math.tan(math.radians(self.angle_tolerance))
        if not math.isfinite(self._slope_scale) or self._slope_scale == 0:
            raise ValueError('time_scale/sample_interval must be representable')
        self.reset()

    @staticmethod
    def _number(name, value):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError(f'{name} must be a finite number')

    @property
    def ready(self) -> bool:
        return len(self._reference.values) == self._reference.size

    @property
    def result(self) -> TrendResult:
        return self._result

    def reset(self):
        self._reference = _TrendWindow(self.data_max_size - self.window)
        self._tail = _TrendWindow(self.window)
        self._memory = deque([0.0] * self.inertia_window, maxlen=self.inertia_window)
        self._mass = 0.0
        self._direction = 0
        self._origin = None
        self._index = -1
        self._event = None
        self._next_scan = 0
        self._qualified = False
        self._result = TrendResult()

    def _breakpoint(self):
        """Best continuous bend in O(W), using centered prefix moments.

        A competing pair of independent lines rejects discontinuous steps.
        Smooth curvature is compared with an orthogonal cubic regression.
        A spike fails minimum segment length / residual tests.
        No future observations or true labels enter this fit.
        """
        values = [*self._reference.values, *self._tail.values]
        n = len(values)
        # Local re-centering also protects SSE comparisons on a long ramp.
        origin = values[0]
        values = [v - origin for v in values]
        sy = math.fsum(values)
        syy = math.fsum(v * v for v in values)
        sxy = math.fsum(i * v for i, v in enumerate(values))

        def fit(count, total, total2, cross):
            center = (count - 1) * 0.5
            slope = (cross - center * total) / (count * (count * count - 1) / 12)
            sse = max(0.0, total2 - total * total / count - slope * (cross - center * total))
            return total / count, slope, sse

        _, _, single_sse = fit(n, sy, syy, sxy)
        # Orthogonal quadratic/cubic terms: a smooth cycle arc must not win
        # merely because it bends. Both smooth and broken models have 4 parameters.
        x = [i - (n - 1) * .5 for i in range(n)]
        x2_mean = (n * n - 1) / 12
        x4_over_x2 = (3 * n * n - 7) / 20
        q = [v * v - x2_mean for v in x]
        c = [v * (v * v - x4_over_x2) for v in x]
        smooth_sse = max(0.0, single_sse
                         - math.fsum(y * v for y, v in zip(values, q)) ** 2 / math.fsum(v*v for v in q)
                         - math.fsum(y * v for y, v in zip(values, c)) ** 2 / math.fsum(v*v for v in c))
        left = left2 = leftxy = 0.0
        best = None
        independent_error = math.inf
        center = (n - 1) * .5
        sxx = n * (n * n - 1) / 12
        cross = sxy - center * sy
        margin = max(4, self.window // 2)
        for k, v in enumerate(values, 1):
            left += v
            left2 += v * v
            leftxy += (k - 1) * v
            if k < self.window or k > n - margin:
                continue
            lm, lb, lsse = fit(k, left, left2, leftxy)
            right = sy - left
            rm, rb, rsse = fit(n - k, right, syy - left2, sxy - leftxy - k * right)
            independent_error = min(independent_error, lsse + rsse)
            # An intersection between adjacent samples is a valid continuous
            # knot too. An integer-only knot would mistake it for a level step.
            difference = rb - lb
            if difference:
                jump = rm - rb * (n-k-1)*.5 - (lm + lb*(k-(k-1)*.5))
                knot = k - jump / difference
                if k-1 <= knot <= k and (best is None or lsse+rsse < best[0]):
                    best = (lsse+rsse, knot, difference)
            # Continuous broken line: y = a + b*x + delta*max(0, x-k).
            m = n - k - 1
            sh = m * (m + 1) * .5
            shh = m * (m + 1) * (2*m + 1) / 6
            xh = k * sh + shh - center * sh
            yh = sxy - leftxy - k * right - sy * sh / n
            denom = shh - sh * sh / n - xh * xh / sxx
            delta = (yh - cross * xh / sxx) / denom
            error = max(0.0, single_sse - delta * delta * denom)
            if best is None or error < best[0]:
                best = (error, k, delta)
        if best is None:
            return None
        error, k, delta = best
        noise = max(self.noise_floor, math.sqrt(error / max(1, n - 3)))
        effect = abs(delta) * min(k, n - k) * .5
        numerical_floor = max(self.noise_floor * self.noise_floor * n, single_sse * 1e-10)
        if (error > .5 * max(single_sse, numerical_floor)
                or error > 1.3 * independent_error + numerical_floor
                or error > .8 * smooth_sse + numerical_floor
                or effect <= self.threshold * noise
                or delta * self._direction <= 0):
            return None
        return round(self._index - n + 1 + k)

    def update(self, value: float) -> TrendResult:
        """Append a uniformly spaced finite sample; return score and horizontal.

        Finite inputs whose centered scaled magnitude exceeds 1e100 are rejected
        before mutation, keeping all moment arithmetic representable.
        """
        self._number('value', value)
        value = float(value)
        origin = value if self._origin is None else self._origin
        centered = (value - origin) / self.value_scale
        if (not math.isfinite(centered) or abs(centered) > 1e100
                or not math.isfinite(centered * self._slope_scale)):
            raise ValueError('centered scaled value must be representable and <= 1e100')
        self._origin = origin
        self._index += 1
        expired = self._tail.append(centered)
        if expired is not None:
            self._reference.append(expired)
        if not self.ready:
            self._result = TrendResult()
            return self._result
        rm, rb, rs = self._reference.fit()
        tm, tb, ts = self._tail.fit()
        gap = self.data_max_size * 0.5
        displacement = tm - (rm + rb * gap)
        delta = tb - rb
        if self.evidence_mode == 'regression':
            reference_slope_error, projected_error = self._reference.uncertainty(gap)
            tail_slope_error, tail_mean_error = self._tail.uncertainty()
            slope_error = max(self.noise_floor, math.hypot(reference_slope_error, tail_slope_error))
            displacement_error = max(self.noise_floor, math.hypot(projected_error, tail_mean_error))
            strength = min(abs(delta) / slope_error, abs(displacement) / displacement_error) / self.threshold
        else:
            sigma = max(rs, ts, self.noise_floor)
            strength = min(abs(delta) * (self.window - 1), abs(displacement)) / sigma / self.threshold
        evidence = min(1.0, max(0.0, strength - 1.0)) if delta * displacement > 0 else 0.0
        direction = (1 if delta > 0 else -1) if evidence else 0
        if direction and direction != self._direction:
            self._memory = deque([0.0] * self.inertia_window, maxlen=self.inertia_window)
            self._mass = 0.0
            self._event = None
            self._qualified = False
            self._next_scan = self._index
        if direction:
            self._direction = direction
        self._mass += evidence - self._memory[0]
        self._memory.append(evidence)
        score = max(0.0, min(1.0, self._mass / self.inertia_window))
        if score < 1e-12:
            score = self._mass = 0.0
            self._event = None
            self._qualified = False
        if score >= 0.8 and not self._qualified and self._index >= self._next_scan:
            start = self._breakpoint()
            self._next_scan = self._index + self.inertia_window
            if start is not None:
                self._qualified = True
                self._event = (start, self._index)
        # Unqualified accumulation remains visible, but cannot imply a stable change.
        if not self._qualified:
            score = min(score, 0.79)
        slope, reference_slope = tb * self._slope_scale, rb * self._slope_scale
        angle = math.degrees(math.atan(slope))
        horizontal = abs(slope) <= self._horizontal_limit + 1e-14
        self._result = TrendResult(score, horizontal, True, slope, reference_slope, angle,
                                   self._direction if score else 0,
                                   self._event[0] if self._event else None,
                                   self._event[1] if self._event else None,
                                   self._qualified and score >= 0.8, evidence)
        return self._result

    def _fresh(self):
        return type(self)(self.window, self.data_max_size,
                          inertia_window=self.inertia_window, threshold=self.threshold,
                          value_scale=self.value_scale, sample_interval=self.sample_interval,
                          time_scale=self.time_scale, angle_tolerance=self.angle_tolerance,
                          noise_floor=self.noise_floor, logger_name=self.logger.name,
                          evidence_mode=self.evidence_mode,
                          logger_props={'level': logging.getLevelName(self.logger.level)})

    def detect_single_seria(self, data: Iterable[float]) -> list[float]:
        """Fresh causal replay, retaining the plotting API used by other detectors."""
        detector = self._fresh()
        return [detector.update(value).score for value in data]

    def detect(self, data: Iterable[float]) -> TrendResult:
        """Return the final score and horizontal flag without mutating this stream."""
        detector = self._fresh()
        for value in data:
            detector.update(value)
        return detector.result

    def score(self, data: Iterable[float]) -> float:
        return self.detect(data).score


@dataclass(slots=True)
class _CusumDelta:
    value: float
    level: float
    suspect: bool


class CusumDetector(Detector):
    """Score anomalous falls/returns with finite inertia, in [0, 1].

    Adaptive mode learns a noise scale from previous first differences and
    uses drift_std_fraction of that scale as its CUSUM allowance.
    A new oscillating regime initially creates a reaction, then becomes ordinary
    background. The response decays gradually within inertia_window. Threshold
    remains the scale of accumulated two-direction evidence, not raw units.

    Sigma mode preserves the previous frozen-background behavior for replay
    compatibility. New UI bindings explicitly select adaptive mode.

    Two CUSUMs accumulate first differences in background-noise units. The
    product of their window maxima qualifies reversals. A second multiplier
    accumulates noise-filtered movement relative to the background level.
    In sigma mode inertia is cleared by a quiet scoring window. In adaptive
    mode only excess movement feeds inertia, and a finite release envelope
    lets a learned regime's reaction fade gradually.
    One-way changes deliberately require a different detector.

    Background is the standard deviation of preceding first differences,
    assumed initially representative. Data in the scoring window are delayed
    before background admission. Sigma mode quarantines exceptional changes;
    adaptive mode admits delayed past changes so the background can learn a
    new regime. fit() can provide an explicit reference history.

    The result is a bounded severity score, not a calibrated probability.
    Before background/window warmup update() returns 0; inspect ready to
    distinguish this from a measured zero. Instances hold one series' state.
    """

    def __init__(self, threshold: float = 5.0, drift: float = 3.0,
                 window: int = 10, *, reference_window: int = 100,
                 min_reference_samples: int = 20, noise_floor: float = 1e-9,
                 update_sigma: float = 8.0, relative_threshold: float = 0.5,
                 relative_drift: float = 0.01, inertia_window: int | None = None,
                 drift_mode: str = 'sigma', drift_quantile: float = 0.95,
                 drift_std_fraction: float = 0.5,
                 logger_name: str | None = None,
                 logger_props: dict[str, str] | None = None):
        for name, value in (('threshold', threshold), ('noise_floor', noise_floor),
                            ('update_sigma', update_sigma),
                            ('relative_threshold', relative_threshold)):
            self._number(name, value)
            if value <= 0:
                raise ValueError(f'{name} must be positive')
        self._number('drift', drift)
        if drift < 0:
            raise ValueError('drift must be nonnegative')
        self._number('relative_drift', relative_drift)
        if relative_drift < 0:
            raise ValueError('relative_drift must be nonnegative')
        for name, value, minimum in (('window', window, 3),
                                      ('reference_window', reference_window, 2),
                                      ('min_reference_samples', min_reference_samples, 2)):
            if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
                raise ValueError(f'{name} must be an integer >= {minimum}')
        if min_reference_samples > reference_window:
            raise ValueError('min_reference_samples must be <= reference_window')
        if inertia_window is None:
            inertia_window = 20 * int(window)
        if (isinstance(inertia_window, bool) or not isinstance(inertia_window, Integral)
                or inertia_window < window):
            raise ValueError('inertia_window must be an integer >= window')
        super().__init__(logger_name, logger_props)
        if drift_mode not in ('sigma', 'adaptive'):
            raise ValueError('drift_mode must be sigma or adaptive')
        self._number('drift_quantile', drift_quantile)
        if not 0 < drift_quantile < 1:
            raise ValueError('drift_quantile must be between 0 and 1')
        self._number('drift_std_fraction', drift_std_fraction)
        if drift_std_fraction <= 0:
            raise ValueError('drift_std_fraction must be positive')
        self.drift_mode, self.drift_quantile = drift_mode, float(drift_quantile)
        self.drift_std_fraction = float(drift_std_fraction)
        self.threshold, self.drift = float(threshold), float(drift)
        self.window = int(window)
        self.reference_window = int(reference_window)
        self.min_reference_samples = int(min_reference_samples)
        self.noise_floor, self.update_sigma = float(noise_floor), float(update_sigma)
        self.relative_threshold = float(relative_threshold)
        self.relative_drift = float(relative_drift)
        self.inertia_window = int(inertia_window)
        self._seed: tuple[float, ...] = ()
        self._seed_levels: tuple[float, ...] = ()
        self.reset()

    @staticmethod
    def _number(name, value):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError(f'{name} must be a finite number')

    @property
    def ready(self) -> bool:
        return (len(self._reference) >= self.min_reference_samples
                and len(self._pending) == self.window - 1)

    @property
    def noise_std(self) -> float | None:
        """Past-delta scale; adaptive mode caps rare tails at its learned quantile."""
        if len(self._reference) < self.min_reference_samples:
            return None
        if self.drift_mode == 'adaptive':
            cap = self._quantile([abs(d) for d in self._reference], self.drift_quantile)
            reference = [max(-cap, min(cap, d)) for d in self._reference]
        else:
            reference = self._reference
        return max(self.noise_floor, pstdev(reference))

    @staticmethod
    def _quantile(values, probability):
        ordered = sorted(values)
        position = probability * (len(ordered) - 1)
        left = int(position)
        right = min(left + 1, len(ordered) - 1)
        return ordered[left] + (ordered[right] - ordered[left]) * (position - left)

    @property
    def background_level(self) -> float | None:
        """Median of the most recent min_reference_samples accepted past levels."""
        if len(self._reference_levels) < self.min_reference_samples:
            return None
        levels = sorted(list(self._reference_levels)[-self.min_reference_samples:])
        middle = len(levels) // 2
        if len(levels) % 2:
            return levels[middle]
        # Average without overflowing two large, finite background levels.
        return levels[middle - 1] * 0.5 + levels[middle] * 0.5

    @property
    def drift_allowance(self) -> float | None:
        """Absolute deadband from preceding admitted deltas, never the scoring window.

        Legacy sigma mode uses drift * sigma. Adaptive mode uses a fraction of
        the preceding standard deviation, not an extreme-jump percentile as
        a hard per-increment gate. Learning a noisy regime lowers its score
        without requiring every ordinary reversal to have exactly zero score.
        """
        sigma = self.noise_std
        if sigma is None:
            return None
        if self.drift_mode == 'sigma':
            return self.drift * sigma
        return max(self.noise_floor, self.relative_drift * abs(self.background_level),
                   self.drift_std_fraction * sigma)

    def reset(self):
        """Clear stream/window state, retaining any explicit fit() background."""
        self._reference = deque(self._seed, maxlen=self.reference_window)
        self._reference_levels = deque(self._seed_levels, maxlen=self.reference_window)
        self._pending: deque[_CusumDelta] = deque()
        self._inertia: deque[tuple[float, float]] = deque(maxlen=self.inertia_window)
        self._response_memory: deque[float] = deque(maxlen=self.inertia_window)
        self._previous: float | None = None
        self._score = 0.0
        self.noise_score = self.inertia_score = self.instant_score = 0.0

    def fit(self, reference: Iterable[float]):
        """Initialize noise from healthy history; return self for chaining.

        At least min_reference_samples + 1 values are required. No edge is
        formed between the last reference value and the first scoring value.
        A later fit() replaces the previous fitted background and stream.
        """
        values = list(reference)
        for value in values:
            self._number('reference value', value)
        if len(values) <= self.min_reference_samples:
            raise ValueError('reference requires min_reference_samples + 1 values')
        deltas = [float(b) - float(a) for a, b in zip(values, values[1:])]
        if any(not math.isfinite(delta) for delta in deltas):
            raise ValueError('reference first differences must be finite')
        self._seed = tuple(deltas[-self.reference_window:])
        self._seed_levels = tuple(float(v) for v in values[1:][-self.reference_window:])
        self.reset()
        return self

    def update(self, value: float) -> float:
        """Append one sample and score only the latest complete sample window."""
        self._number('value', value)
        value = float(value)
        if self._previous is None:
            self._previous = value
            return 0.0
        delta = value - self._previous
        if not math.isfinite(delta):
            raise ValueError('first difference must be finite')

        if len(self._reference) < self.min_reference_samples:
            self._reference.append(delta)
            self._reference_levels.append(value)
            self._previous = value
            return 0.0

        # Commit only history outside the current window. Alarm decisions can
        # quarantine a whole pending window before any of it becomes reference.
        if len(self._pending) == self.window - 1:
            expired = self._pending.popleft()
            if self.drift_mode == 'adaptive' or not expired.suspect:
                self._reference.append(expired.value)
                self._reference_levels.append(expired.level)
        sigma = self.noise_std
        drift_allowance = self.drift_allowance
        scale = sigma
        suspect = abs(delta) / sigma > self.update_sigma
        self._pending.append(_CusumDelta(delta, value, suspect))
        level_scale = max(abs(self.background_level), sigma)
        # Adaptive mass uses the same allowance as the CUSUM; legacy mass
        # discounts one noise std, retaining gradual parts of a recovery.
        allowance = (max(drift_allowance, self.relative_drift * level_scale) if self.drift_mode == 'adaptive'
                     else max(sigma, self.relative_drift * level_scale))
        excess = max(0.0, abs(delta) - allowance)
        relative_movement = min(1e6, excess / level_scale)
        self._inertia.append((relative_movement if delta > 0 else 0.0,
                              relative_movement if delta < 0 else 0.0))
        self._previous = value
        if not self.ready:
            return 0.0

        positive = negative = peak_positive = peak_negative = 0.0
        for sample in self._pending:
            # Clipping only prevents overflow: such an excursion already
            # saturates the score once a reverse excursion is present.
            if self.drift_mode == 'adaptive':
                # Subtract the absolute deadband before clipping; a tiny scale
                # must not hide real jumps through two independently clipped terms.
                up = max(-1e6, min(1e6, (sample.value - drift_allowance) / scale))
                down = max(-1e6, min(1e6, (-sample.value - drift_allowance) / scale))
                positive = max(0.0, positive + up)
                negative = max(0.0, negative + down)
            else:
                z = max(-1e6, min(1e6, sample.value / sigma))
                positive = max(0.0, positive + z - self.drift)
                negative = max(0.0, negative - z - self.drift)
            peak_positive = max(peak_positive, positive)
            peak_negative = max(peak_negative, negative)
        # E = sqrt(peak_positive * peak_negative) / threshold. This ordering
        # avoids squaring very large/small valid threshold values.
        evidence = math.sqrt(peak_positive) * math.sqrt(peak_negative) / self.threshold
        noise_score = -math.expm1(-evidence * evidence)
        if peak_positive == 0.0 and peak_negative == 0.0 and self.drift_mode == 'sigma':
            # Quiet separation starts a new episode. A previous large burst
            # must not inflate an unrelated shallow dip after recovery.
            self._inertia.clear()
        up_mass = math.fsum(up for up, _ in self._inertia)
        down_mass = math.fsum(down for _, down in self._inertia)
        relative_evidence = math.sqrt(up_mass) * math.sqrt(down_mass) / self.relative_threshold
        inertia_score = -math.expm1(-relative_evidence * relative_evidence)
        raw_score = noise_score * inertia_score
        self.noise_score, self.inertia_score, self.instant_score = noise_score, inertia_score, raw_score
        if self.drift_mode == 'adaptive':
            # Finite, linearly decaying response memory: once a new regime is
            # learned, ordinary repetitions cannot refresh the anomaly response.
            self._response_memory.append(raw_score)
            size = len(self._response_memory)
            self._score = max(score * (1.0 - (size - 1 - i) / self.inertia_window)
                              for i, score in enumerate(self._response_memory))
        else:
            self._score = raw_score
        # Unexpected movements must not train the noise estimate even when
        # their short duration or small relative depth keeps severity low.
        if noise_score >= 0.5 and self.drift_mode == 'sigma':
            for sample in self._pending:
                sample.suspect = True
        return self._score

    def _fresh(self):
        detector = type(self)(self.threshold, self.drift, self.window,
                              reference_window=self.reference_window,
                              min_reference_samples=self.min_reference_samples,
                              noise_floor=self.noise_floor, update_sigma=self.update_sigma,
                              relative_threshold=self.relative_threshold,
                              relative_drift=self.relative_drift,
                              inertia_window=self.inertia_window,
                              drift_mode=self.drift_mode, drift_quantile=self.drift_quantile,
                              drift_std_fraction=self.drift_std_fraction,
                              logger_name=self.logger.name,
                              logger_props={'level': logging.getLevelName(self.logger.level)})
        detector._seed = self._seed
        detector._seed_levels = self._seed_levels
        detector.reset()
        return detector

    def detect_single_seria(self, data: Iterable[float]) -> list[float]:
        """Causal batch replay: one window score per input point, without mutation.

        Retains the original detector's plotting entry point. Uses constructor
        settings and an optional fit() seed, never the active stream's history.
        """
        detector = self._fresh()
        return [detector.update(value) for value in data]

    def detect(self, data: Iterable[float]) -> float:
        """Return the final window score after replaying data as a fresh series."""
        scores = self.detect_single_seria(data)
        return scores[-1] if scores else 0.0

    def score(self, data: Iterable[float]) -> float:
        """Alias for detect(); use fit(history).score(window) with explicit context."""
        return self.detect(data)


class MadDetector(Detector):
    """Adaptive dispersion-change score using mean absolute deviation.

    Compare the latest ``window`` samples with the latest ``data_max_size``
    samples, including the local window. Both MADs are deviations from their
    own arithmetic mean, not median absolute deviations. The reference rolls
    with the stream, so the score settles after a new regime is established.

    Defaults reproduce the test's intended 20/100 windows, without its slice
    boundary error. update() is causal and returns 0 until the entire reference
    is available; ready distinguishes this warmup from a measured zero. Scores
    are bounded severity values, not probabilities or binary alarm decisions.
    One instance holds one series' state.
    """

    def __init__(self, window: int = 20, data_max_size: int = 100, *,
                 logger_name: str | None = None,
                 logger_props: dict[str, str] | None = None):
        for name, value in (('window', window), ('data_max_size', data_max_size)):
            if isinstance(value, bool) or not isinstance(value, Integral) or value < 2:
                raise ValueError(f'{name} must be an integer >= 2')
        if data_max_size < window:
            raise ValueError('data_max_size must be >= window')
        super().__init__(logger_name, logger_props)
        self.window = int(window)
        self.data_max_size = int(data_max_size)
        self.reset()

    @property
    def ready(self) -> bool:
        return len(self._reference) == self.data_max_size

    @property
    def local_mad(self) -> float | None:
        """Latest local mean absolute deviation, or None before warmup."""
        return self._local_mad

    @property
    def reference_mad(self) -> float | None:
        """Latest rolling-reference deviation, or None before warmup."""
        return self._reference_mad

    def reset(self):
        """Clear the reference, cached deviations and score."""
        self._reference: deque[float] = deque(maxlen=self.data_max_size)
        self._local_mad: float | None = None
        self._reference_mad: float | None = None
        self._score = 0.0

    @staticmethod
    def _mean_absolute_deviation(values: list[float]) -> float:
        # Center first to retain precision for small noise on a large level.
        # Divide before summing/subtracting to avoid intermediate overflow.
        count = len(values)
        shifted = [value - values[0] for value in values]
        scale = 1.0
        if any(not math.isfinite(value) for value in shifted):
            scale = max(abs(value) for value in values)
            shifted = [value / scale for value in values]
        mean = math.fsum(value / count for value in shifted)
        return math.fsum(abs(value / count - mean / count)
                         for value in shifted) * scale

    def update(self, value: float) -> float:
        """Append one finite observation and score the current 20/100 windows."""
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError('value must be a finite number')
        self._reference.append(float(value))
        if not self.ready:
            return 0.0
        reference = list(self._reference)
        self._local_mad = self._mean_absolute_deviation(reference[-self.window:])
        self._reference_mad = self._mean_absolute_deviation(reference)
        if self._reference_mad == 0.0:
            # A constant reference also has a constant local window. The
            # second branch handles underflow at subnormal magnitudes.
            self._score = 0.0 if self._local_mad == 0.0 else 1.0
        else:
            relative = abs(self._local_mad - self._reference_mad) / self._reference_mad
            # Exactly the original 2/(1+exp(-relative))-1 normalization,
            # expressed without cancellation for small relative changes.
            self._score = math.tanh(relative * 0.5)
        return self._score

    def detect_single_seria(self, data: Iterable[float]) -> list[float]:
        """Replay a fresh causal stream, without changing this instance.

        Return exactly one score per input, including the warmup zeros and the
        final observation. Constructor settings are retained between replays.
        """
        detector = type(self)(self.window, self.data_max_size,
                              logger_name=self.logger.name,
                              logger_props={'level': logging.getLevelName(self.logger.level)})
        return [detector.update(value) for value in data]

    def detect(self, data: Iterable[float]) -> float:
        """Final score of a fresh replay, or 0 for an empty input."""
        scores = self.detect_single_seria(data)
        return scores[-1] if scores else 0.0

    def score(self, data: Iterable[float]) -> float:
        """Alias for detect()."""
        return self.detect(data)


@dataclass(frozen=True, slots=True)
class SSAResult:
    """SSA reconstruction magnitude, not an anomaly probability.

    deformation is the RMS orthogonal residual / reference sigma, before
    score_scale compression. A noisy stationary series may have a nonzero
    background. reference_end is the last training index (zero-based), strictly
    before the current window; refit_age counts samples since the last fit.
    Unrepresentable diagnostic magnitudes saturate at the largest float.
    """

    ready: bool = False
    score: float = 0.0
    deformation: float = 0.0
    residual_rms: float | None = None
    reference_sigma: float | None = None
    reference_deformation: float | None = None
    lag: int = 0
    rank: int = 0
    reference_size: int = 0
    reference_end: int | None = None
    refit_age: int = 0
    zero_reference_variance: bool = False


class SSADetector(Detector):
    """Bounded magnitude of departure from a preceding rolling SSA model.

    The last data_max_size observations split into a reference of
    data_max_size-window samples and a DISJOINT current window. An SSA Hankel
    model learns reference location, sigma and leading lag subspace. The RMS
    orthogonal residual of all current lag vectors, divided by reference sigma,
    gives D; score = D / (D + score_scale). A D equal to score_scale gives 0.5.
    The current window never trains its own model or normalization. Models are
    refreshed every refit_interval samples, so reference_end may be older still.

    The reference rolls: an established new regime eventually replaces the old
    one and the score returns towards its NEW reconstruction background. This
    intentionally differs from the experiment's permanent healthy reference.
    score measures reconstruction magnitude, not change significance; stationary
    noise is not perfectly reconstructible and need not have score zero.
    refit_interval=None freezes the first model instead, retaining departures
    from that original regime as in the experiment.

    lag=None selects the first strong reference ACF peak (8..min(128,window/2)),
    otherwise min(32,window/2). Rank covers energy of reference lag vectors,
    capped by max_rank and lag-1. Shape changes inside the same harmonic space
    may be invisible after their transition. Equally spaced scalar input only.

    One instance per series. Warmup returns zero until data_max_size samples;
    ready distinguishes warmup from a measured zero. Persistent state is
    O(data_max_size + lag*rank); refit scratch is O(reference_window*lag+lag**2).
    Current scoring is O(window*lag*rank); a refit adds
    O(reference_window*lag**2 + lag**3), amortized over refit_interval calls.
    """

    def __init__(self, window: int = 192, data_max_size: int = 960, *,
                 lag: int | None = None, energy: float = 0.95,
                 max_rank: int = 12, refit_interval: int | None = 16,
                 score_scale: float = 1.0,
                 logger_name: str | None = None,
                 logger_props: dict[str, str] | None = None):
        for name, value, minimum in (('window', window, 8),
                                     ('data_max_size', data_max_size, 24),
                                     ('max_rank', max_rank, 1)):
            if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
                raise ValueError(f'{name} must be an integer >= {minimum}')
        if data_max_size < 3 * window:
            raise ValueError('data_max_size must be >= 3 * window')
        if refit_interval is not None and (isinstance(refit_interval, bool)
                or not isinstance(refit_interval, Integral) or refit_interval < 1):
            raise ValueError('refit_interval must be None or an integer >= 1')
        if lag is not None:
            if isinstance(lag, bool) or not isinstance(lag, Integral) or not 2 <= lag <= window // 2:
                raise ValueError('lag must be None or an integer in [2, window // 2]')
        for name, value in (('energy', energy), ('score_scale', score_scale)):
            self._number(name, value)
        if not 0 < energy < 1:
            raise ValueError('energy must be in (0, 1)')
        if score_scale <= 0:
            raise ValueError('score_scale must be positive')
        super().__init__(logger_name, logger_props)
        self.window, self.data_max_size = int(window), int(data_max_size)
        self.reference_window = self.data_max_size - self.window
        self.lag = None if lag is None else int(lag)
        self.energy, self.max_rank = float(energy), int(max_rank)
        self.refit_interval = None if refit_interval is None else int(refit_interval)
        self.score_scale = float(score_scale)
        self.reset()

    @staticmethod
    def _number(name, value):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError(f'{name} must be a finite number')

    @property
    def ready(self) -> bool:
        return self._result.ready

    @property
    def result(self) -> SSAResult:
        return self._result

    def reset(self):
        self._values: deque[float] = deque(maxlen=self.data_max_size)
        self._index = -1
        self._fitted_at = None
        self._model = None
        self._result = SSAResult()

    @staticmethod
    def _rms(values):
        """Scaled sum of squares avoids under/overflow for finite coordinates."""
        size = float(np.max(np.abs(values)))
        if size == 0:
            return 0.0
        return size * math.sqrt(float(np.mean((values / size) ** 2)))

    def _choose_lag(self, centered):
        if self.lag is not None:
            return self.lag
        upper = min(128, self.window // 2)
        fallback = min(32, upper)
        size = float(np.max(np.abs(centered)))
        if size == 0:
            return fallback
        y = centered / size
        # Only inspected lags are evaluated; input is always the reference.
        den = float(y @ y)
        acf = np.array([float(y[k:] @ y[:len(y)-k]) / den
                        for k in range(upper + 2)])
        peaks, _ = find_peaks(acf)
        peaks = peaks[(peaks >= 8) & (peaks <= upper) & (acf[peaks] > .5)]
        return int(peaks[0]) if len(peaks) else fallback

    def _fit_reference(self, reference):
        origin = float(reference[0])
        with np.errstate(over='ignore'):
            shifted = reference - origin
        if not np.isfinite(shifted).all():
            origin = 0.0
            unit = float(np.max(np.abs(reference)))
            coordinates = reference / unit
        else:
            unit = float(np.max(np.abs(shifted))) or 1.0
            coordinates = shifted / unit
        offset = float(coordinates.mean())
        centered = coordinates - offset
        sigma = self._rms(centered)
        lag = self._choose_lag(centered)
        X = np.lib.stride_tricks.sliding_window_view(centered, lag)
        if sigma == 0:
            basis = np.empty((lag, 0))
        else:
            covariance = X.T @ X / len(X)
            eigenvalues, eigenvectors = eigh(covariance, check_finite=False)
            eigenvalues = np.maximum(eigenvalues[::-1], 0)
            rank = int(np.searchsorted(np.cumsum(eigenvalues), self.energy * eigenvalues.sum()) + 1)
            rank = min(rank, self.max_rank, lag - 1)
            basis = eigenvectors[:, -rank:]
        residual = X - (X @ basis) @ basis.T
        background = self._ratio(self._rms(residual), sigma)
        return dict(origin=origin, unit=unit, offset=offset, sigma=sigma,
                    lag=lag, basis=basis, background=background,
                    reference_end=self._index - self.window)

    @staticmethod
    def _ratio(numerator, denominator):
        if numerator == 0:
            return 0.0
        if denominator == 0:
            return float(np.finfo(float).max)
        value = numerator / denominator
        return value if math.isfinite(value) else float(np.finfo(float).max)

    @staticmethod
    def _magnitude(value, unit):
        if value == 0:
            return 0.0
        return min(float(np.finfo(float).max), value * unit)

    def _evaluate(self, current):
        model = self._model
        with np.errstate(over='ignore'):
            shifted = current - model['origin']
        if np.isfinite(shifted).all():
            unit = max(model['unit'], float(np.max(np.abs(shifted))))
            centered = shifted / unit - model['offset'] * (model['unit'] / unit)
        else:
            unit = max(model['unit'], abs(model['origin']), float(np.max(np.abs(current))))
            centered = (current / unit - model['origin'] / unit
                        - model['offset'] * (model['unit'] / unit))
        X = np.lib.stride_tricks.sliding_window_view(centered, model['lag'])
        basis = model['basis']
        rms = self._rms(X - (X @ basis) @ basis.T)
        sigma = model['sigma'] * (model['unit'] / unit)
        deformation = self._ratio(rms, sigma)
        # Algebraically D/(D+score_scale); both branches avoid overflow.
        if deformation >= self.score_scale:
            score = 1.0 / (1.0 + self.score_scale / deformation)
        else:
            scaled = deformation / self.score_scale
            score = scaled / (1.0 + scaled)
        return SSAResult(
            ready=True, score=score, deformation=deformation,
            residual_rms=self._magnitude(rms, unit),
            reference_sigma=self._magnitude(model['sigma'], model['unit']),
            reference_deformation=model['background'], lag=model['lag'],
            rank=basis.shape[1], reference_size=self.reference_window,
            reference_end=model['reference_end'], refit_age=self._index - self._fitted_at,
            zero_reference_variance=model['sigma'] == 0)

    def update(self, value: float) -> float:
        """Score one sample after a complete disjoint reference/current warmup."""
        self._number('value', value)
        self._values.append(float(value))
        self._index += 1
        if len(self._values) < self.data_max_size:
            return 0.0
        values = np.array(self._values)
        if (self._model is None or (self.refit_interval is not None
                and self._index - self._fitted_at >= self.refit_interval)):
            self._model = self._fit_reference(values[:-self.window])
            self._fitted_at = self._index
        self._result = self._evaluate(values[-self.window:])
        return self._result.score

    def _fresh(self):
        return type(self)(self.window, self.data_max_size, lag=self.lag,
                          energy=self.energy, max_rank=self.max_rank,
                          refit_interval=self.refit_interval, score_scale=self.score_scale,
                          logger_name=self.logger.name,
                          logger_props={'level': logging.getLevelName(self.logger.level)})

    def detect_single_seria(self, data: Iterable[float]) -> list[float]:
        """One causal score per point; never mutate the active stream."""
        detector = self._fresh()
        return [detector.update(value) for value in data]

    def analyze(self, data: Iterable[float]) -> SSAResult:
        """Final diagnostics of a fresh replay; incomplete input is not ready."""
        detector = self._fresh()
        for value in data:
            detector.update(value)
        return detector.result

    def detect(self, data: Iterable[float]) -> float:
        return self.analyze(data).score

    def score(self, data: Iterable[float]) -> float:
        return self.detect(data)


@dataclass(frozen=True, slots=True)
class ACFResult:
    """Snapshot of a lag-profile change; all lag arrays include lag zero.

    depth is the last significant lag in samples, not an event's age or a
    causal forecast. lag_mass sums threshold-exceeding changes, capped at one
    per lag; score = lag_mass / max_lag. horizon_reached marks a significant
    change at the largest inspected lag. Zero-energy profiles use all zeros
    (a convention, not a defined statistical ACF).
    """

    ready: bool = False
    score: float = 0.0
    depth: int = 0
    lag_mass: float = 0.0
    horizon_reached: bool = False
    acf: tuple[float, ...] = ()
    reference_acf: tuple[float, ...] = ()
    lag_changes: tuple[float, ...] = ()
    thresholds: tuple[float, ...] = ()
    significant_lags: tuple[int, ...] = ()
    zero_energy: bool = False
    reference_zero_energy: bool = False


class ACFDetector(Detector):
    """Measure changed temporal persistence relative to a healthy reference.

    Both windows are centered on their OWN means: a level transition is visible
    while it occupies the local window; a settled plateau becomes the new norm.
    The biased sample ACF uses a common lag-zero energy denominator (no n-k
    correction), independently of the measurement's units or level.

    Each signed profile is compared with the fixed healthy profile. A
    Bartlett-inspired noise allowance and min_change suppress sampling noise;
    these are heuristic sensitivity controls, not calibrated significance
    levels. No amplitude severity, anomaly type or causal effect is inferred.

    fit() supplies healthy history. Otherwise the first reference_window
    observations train the reference and the NEXT window observations warm up
    scoring. The reference never learns from scored observations. Uniformly
    spaced finite scalar samples are required; one instance holds one series.
    """

    def __init__(self, window: int = 256, max_lag: int = 32, *,
                 reference_window: int = 512, threshold: float = 3.5,
                 min_change: float = 0.1,
                 logger_name: str | None = None,
                 logger_props: dict[str, str] | None = None):
        for name, value, minimum in (('window', window, 8),
                                     ('max_lag', max_lag, 1),
                                     ('reference_window', reference_window, 8)):
            if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
                raise ValueError(f'{name} must be an integer >= {minimum}')
        if 4 * max_lag > window:
            raise ValueError('window must be >= 4 * max_lag')
        if reference_window < window:
            raise ValueError('reference_window must be >= window')
        for name, value in (('threshold', threshold), ('min_change', min_change)):
            self._number(name, value)
        if threshold <= 0:
            raise ValueError('threshold must be positive')
        if not 0 <= min_change < 2:
            raise ValueError('min_change must be in [0, 2)')
        super().__init__(logger_name, logger_props)
        self.window, self.max_lag = int(window), int(max_lag)
        self.reference_window = int(reference_window)
        self.threshold, self.min_change = float(threshold), float(min_change)
        self._seed: tuple[float, ...] = ()
        self.reset()

    @staticmethod
    def _number(name, value):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError(f'{name} must be a finite number')

    @staticmethod
    def _location(values):
        # Retain low-amplitude variation on a large DC level. In the extreme
        # opposite-sign case, use scaled coordinates to avoid subtraction overflow.
        origin = values[0]
        shifted = [v - origin for v in values]
        unit = 1.0
        if any(not math.isfinite(v) for v in shifted):
            origin = 0.0
            unit = max(abs(v) for v in values)
            shifted = [v / unit for v in values]
        return origin, math.fsum(v / len(values) for v in shifted), unit

    def _profile(self, values, location):
        origin, offset, unit = location
        residuals = [(v - origin) / unit - offset for v in values]
        if any(not math.isfinite(v) for v in residuals):
            scale = max(max(abs(v) for v in values), abs(origin), unit, abs(offset))
            residuals = [v / scale - origin / scale - offset * (unit / scale)
                         for v in values]
        scale = max(abs(v) for v in residuals)
        if scale == 0.0:
            return (0.0,) * (self.max_lag + 1), True
        residuals = [v / scale for v in residuals]
        energy = math.fsum(v * v for v in residuals)
        profile = [1.0]
        for lag in range(1, self.max_lag + 1):
            product = math.fsum(a * b for a, b in zip(residuals, residuals[lag:]))
            profile.append(max(-1.0, min(1.0, product / energy)))
        return tuple(profile), False

    def _set_reference(self):
        values = list(self._reference)
        self._reference_acf, self._reference_zero = self._profile(
            values, self._location(values))
        # Approximate null variability of two independent profile estimates.
        # Use only the healthy profile; abnormal persistence must not inflate
        # its own allowance. This is not a formal sequential hypothesis test.
        squares, bands = 0.0, [0.0]
        for lag in range(1, self.max_lag + 1):
            noise = math.sqrt((1 + 2 * squares) * (1 / self.window + 1 / len(values)))
            bands.append(min(2.0, max(self.min_change, self.threshold * noise)))
            squares += self._reference_acf[lag] ** 2
        self._thresholds = tuple(bands)

    @property
    def reference_ready(self) -> bool:
        return bool(self._reference_acf)

    @property
    def ready(self) -> bool:
        return self._result.ready

    @property
    def result(self) -> ACFResult:
        """Latest immutable diagnostics; ready=False distinguishes warmup zeros."""
        return self._result

    def reset(self):
        """Clear stream state; retain only an explicitly fitted reference."""
        self._reference = deque(self._seed, maxlen=self.reference_window)
        self._values: deque[float] = deque(maxlen=self.window)
        self._reference_acf: tuple[float, ...] = ()
        self._reference_zero = False
        self._thresholds: tuple[float, ...] = ()
        self._result = ACFResult()
        if self._seed:
            self._set_reference()

    def fit(self, reference: Iterable[float]):
        """Replace background and reset; retain at most reference_window values.

        At least window healthy points are required. Scoring starts after a
        separate full local window; there is no reference-to-stream edge.
        Invalid input leaves both background and active stream unchanged.
        """
        values = []
        for value in reference:
            self._number('reference value', value)
            values.append(float(value))
        if len(values) < self.window:
            raise ValueError('reference requires at least window values')
        self._seed = tuple(values[-self.reference_window:])
        self.reset()
        return self

    def update(self, value: float) -> float:
        """Append one observation and return the latest bounded persistence score."""
        self._number('value', value)
        value = float(value)
        if not self.reference_ready:
            self._reference.append(value)
            if len(self._reference) == self.reference_window:
                self._set_reference()
            return 0.0
        self._values.append(value)
        if len(self._values) < self.window:
            return 0.0
        values = list(self._values)
        profile, zero = self._profile(values, self._location(values))
        changes = tuple(abs(a - b) for a, b in zip(profile, self._reference_acf))
        significant = tuple(k for k in range(1, self.max_lag + 1)
                            if changes[k] > self._thresholds[k])
        mass = math.fsum(min(1.0, changes[k] - self._thresholds[k]) for k in significant)
        depth = significant[-1] if significant else 0
        self._result = ACFResult(
            ready=True, score=mass / self.max_lag, depth=depth, lag_mass=mass,
            horizon_reached=depth == self.max_lag, acf=profile,
            reference_acf=self._reference_acf, lag_changes=changes,
            thresholds=self._thresholds, significant_lags=significant,
            zero_energy=zero, reference_zero_energy=self._reference_zero)
        return self._result.score

    def _fresh(self):
        detector = type(self)(self.window, self.max_lag,
                              reference_window=self.reference_window,
                              threshold=self.threshold, min_change=self.min_change,
                              logger_name=self.logger.name,
                              logger_props={'level': logging.getLevelName(self.logger.level)})
        detector._seed = self._seed
        detector.reset()
        return detector

    def detect_single_seria(self, data: Iterable[float]) -> list[float]:
        """Fresh causal replay with one score per input, without stream mutation."""
        detector = self._fresh()
        return [detector.update(value) for value in data]

    def analyze(self, data: Iterable[float]) -> ACFResult:
        """Final diagnostics of a fresh replay, preserving the active stream."""
        detector = self._fresh()
        for value in data:
            detector.update(value)
        return detector.result

    def detect(self, data: Iterable[float]) -> float:
        """Final score of a fresh replay; zero for empty/incomplete input."""
        return self.analyze(data).score

    def score(self, data: Iterable[float]) -> float:
        """Alias for detect()."""
        return self.detect(data)


@dataclass(frozen=True, slots=True)
class CorrelationPairResult:
    """CCF diagnostics for one learned pair; positive lag means j follows i."""

    pair: tuple[int, int]
    reference_lag: int | None
    reference_correlation: float | None
    correlation: float | None
    change: float
    score: float
    ccf: tuple[float | None, ...]
    reference_ccf: tuple[float | None, ...]
    uninformative: bool = False
    monitored: bool = True
    raw_correlation: float | None = None
    alignment_shift: int = 0
    unaligned_score: float = 0.0
    aligned_correlation: float | None = None
    relative_error: float | None = None
    relative_weight: float = 1.0
    correlation_score: float = 0.0


@dataclass(frozen=True, slots=True)
class CorrelationResult:
    """Change of an established inter-series relationship, not a probability.

    All CCF arrays use lags -max_lag..max_lag. score is the maximum pair
    score averaged over persistence updates. Warmup has ready=False.
    """

    ready: bool = False
    score: float = 0.0
    raw_score: float = 0.0
    detected: bool = False
    pairs: tuple[CorrelationPairResult, ...] = ()
    affected_series: tuple[int, ...] = ()
    provisional: bool = False
    noise_floors: tuple[float, ...] = ()
    score_index: int | None = None
    observed_index: int | None = None
    alignment_delay: int = 0
    pending: bool = False


class CorrelationDetector(Detector):
    """Online interaction change, weighted by relative level and scrape skew.

    Time-major update([x_t,y_t,...]); no labelled healthy interval is needed.
    A growing short-window Pearson CCF is compared with a slow learned CCF.
    min_samples=5 starts estimated scores; provisional marks a growing window.
    Pairs acquire a reference only after consistent signed correlation in two
    disjoint parts of the window, with variation across several increments.

    alignment_tolerance=0 is strict timing. A value of 1 searches coherent
    window shifts -1,0,+1 around the learned lag for each emitted target.
    A single offset is used within that window, avoiding arbitrary pointwise
    noise fitting. Right-neighbor use delays issuance by that many samples.
    score_index identifies the target; observed_index identifies issue time.
    No right halo is fabricated at EOF. replay returns one emission per input;
    its final score targets an earlier sample when tolerance is nonzero.

    Reference/training gates use only strict data through the target; its
    right halo affects the delayed score only. The established lag is not
    retargeted outside the allowed tolerance. Broken relationships do not
    train their own reference. Initial coherence is not proof of health.

    Relative size is measured against the learned affine relationship in
    BOTH directions. Each residual is divided by the expected CURRENT level
    (prior std is a fallback near zero), clipped at 200%, then aggregated as
    an RMS across the aligned window and directions. relative_change_scale
    controls weight=e/(e+scale): default .1 means a 10% mismatch has weight .5.
    Zero disables this weight. Normal offset/gain is part of the learned
    prediction. Thus 15 units around 400 weigh more than 25 around 800.

    Pair score = clipped CCF change beyond min_change, times an activity
    weight and this relative weight. Activity uses max(noise_floor,
    relative_noise_floor*prior_std); evidence rises from zero at this floor
    to one at 3*floor. One stopped member counts as loss of an established
    link; two uninformative members carry no evidence. Global score is the
    persistence-point mean of maximum pair scores, not a probability.

    Raw CCF, selected aligned correlation/shift, relative error/weight and
    correlation-only severity are exposed separately. Off-peak CCF shape
    changes alone do not alarm: a common step changes the common signal's ACF.
    A settled level/gain change with intact correlation can be invisible.

    fit is optional past history. Batch methods preserve only explicit fit
    history and do not mutate the active stream. Samples must be uniformly
    spaced and columns aligned in fixed order. Memory is bounded; update is
    O(pairs*(lags+alignment_tolerance)*window). relative_change_scale applies
    to adaptive=True. adaptive=False retains the explicit frozen-reference
    algorithm and requires alignment_tolerance=0.
    """

    def __init__(self, window: int = 32, max_lag: int = 4, *,
                 reference_window: int = 128, min_correlation: float = 0.7,
                 min_change: float = 0.25, persistence: int = 3,
                 noise_floor: float = 0.0, adaptive: bool = True,
                 min_samples: int = 5, relative_noise_floor: float = 0.3,
                 alignment_tolerance: int = 0,
                 relative_change_scale: float = 0.1,
                 alert_threshold: float = 0.4,
                 logger_name: str | None = None,
                 logger_props: dict[str, str] | None = None):
        for name, value, minimum in (('window', window, 8), ('max_lag', max_lag, 0),
                                     ('reference_window', reference_window, 8),
                                     ('persistence', persistence, 1),
                                     ('min_samples', min_samples, 4),
                                     ('alignment_tolerance', alignment_tolerance, 0)):
            if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
                raise ValueError(f'{name} must be an integer >= {minimum}')
        if 4 * max_lag > window or reference_window < window:
            raise ValueError('require window >= 4*max_lag and reference_window >= window')
        if min_samples > window:
            raise ValueError('min_samples must be <= window')
        if alignment_tolerance > window//4 or alignment_tolerance > min_samples-3:
            raise ValueError('alignment_tolerance must be <= window//4 and min_samples-3')
        if not isinstance(adaptive, bool):
            raise ValueError('adaptive must be a bool')
        if not adaptive and alignment_tolerance:
            raise ValueError('alignment_tolerance requires adaptive=True')
        for name, value in (('min_correlation', min_correlation),
                            ('min_change', min_change), ('alert_threshold', alert_threshold),
                            ('noise_floor', noise_floor),
                            ('relative_noise_floor', relative_noise_floor)):
            ACFDetector._number(name, value)
        ACFDetector._number('relative_change_scale',relative_change_scale)
        if not 0 < min_correlation <= 1 or not 0 <= min_change < 1:
            raise ValueError('require min_correlation in (0,1] and min_change in [0,1)')
        if not 0 < alert_threshold <= 1:
            raise ValueError('alert_threshold must be in (0,1]')
        if noise_floor < 0:
            raise ValueError('noise_floor must be nonnegative')
        if not 0 <= relative_noise_floor < 1:
            raise ValueError('relative_noise_floor must be in [0,1)')
        if relative_change_scale<0:
            raise ValueError('relative_change_scale must be nonnegative')
        super().__init__(logger_name, logger_props)
        self.window, self.max_lag = int(window), int(max_lag)
        self.reference_window, self.persistence = int(reference_window), int(persistence)
        self.min_correlation, self.min_change = float(min_correlation), float(min_change)
        self.alert_threshold = float(alert_threshold)
        self.noise_floor = float(noise_floor)
        self.adaptive, self.min_samples = adaptive, int(min_samples)
        self.relative_noise_floor = float(relative_noise_floor)
        self.alignment_tolerance = int(alignment_tolerance)
        self.relative_change_scale = float(relative_change_scale)
        self._seed: tuple[tuple[float, ...], ...] = ()
        self.reset()

    @staticmethod
    def _row(value, width=None):
        try:
            row = tuple(value)
        except TypeError as error:
            raise ValueError('each observation must contain at least two finite numbers') from error
        if len(row) < 2 or (width is not None and len(row) != width):
            raise ValueError('observations must have the same number of series, at least two')
        for entry in row:
            ACFDetector._number('series value', entry)
        return tuple(float(entry) for entry in row)

    def _center(self, values, floor=None):
        # Subtract first to preserve small variation on a large DC level;
        # fall back to scaled coordinates for opposite-sign extreme inputs.
        with np.errstate(over='ignore', invalid='ignore'):
            centered = values - values[0]
        outer_unit = 1.0
        if not np.isfinite(centered).all():
            outer_unit = np.max(np.abs(values))
            centered = values / outer_unit
        unit = np.max(np.abs(centered))
        if unit == 0:
            return None
        centered = centered / unit
        centered -= centered.mean()
        energy = float(centered @ centered)
        floor = self.noise_floor if floor is None else floor
        if math.sqrt(energy / len(values)) <= (floor / outer_unit) / unit:
            return None
        return centered / math.sqrt(energy) if energy > 0 else None

    def _profile(self, values, i, j, floors=None):
        profile, constants = [], []
        for lag in range(-self.max_lag, self.max_lag + 1):
            if lag > 0:
                x, y = values[:-lag, i], values[lag:, j]
            elif lag < 0:
                x, y = values[-lag:, i], values[:lag, j]
            else:
                x, y = values[:, i], values[:, j]
            if len(x) < 3:
                profile.append(None)
                constants.append((True, True))
                continue
            x = self._center(x, None if floors is None else floors[i])
            y = self._center(y, None if floors is None else floors[j])
            constants.append((x is None, y is None))
            profile.append(None if x is None or y is None else
                           max(-1.0, min(1.0, float(x @ y))))
        return tuple(profile), constants

    def _learn(self, rows):
        values = np.asarray(rows, dtype=float)
        learned = []
        for i in range(values.shape[1]):
            for j in range(i + 1, values.shape[1]):
                profile, _ = self._profile(values, i, j)
                candidates = [k for k, r in enumerate(profile) if r is not None]
                if not candidates:
                    continue
                peak = max(abs(profile[k]) for k in candidates)
                # Avoid assigning a spurious delay to a broad/noisy reference peak.
                near_peak = [k for k in candidates if abs(profile[k]) >= max(peak - .02, self.min_correlation)]
                if not near_peak:
                    continue
                k = min(near_peak, key=lambda k: (abs(k-self.max_lag), -abs(profile[k])))
                if abs(profile[k]) >= self.min_correlation:
                    learned.append((i, j, k, profile))
        if not learned:
            raise ValueError('reference contains no sufficiently correlated pair')
        return tuple(learned)

    @property
    def reference_ready(self) -> bool:
        if self.adaptive:
            return bool(self._anchors)
        return bool(self._learned)

    @property
    def ready(self) -> bool:
        return self._result.ready

    @property
    def result(self) -> CorrelationResult:
        return self._result

    def reset(self):
        """Clear active stream, preserving only explicitly fitted history."""
        self._width = len(self._seed[0]) if self._seed else None
        self._reference = deque(maxlen=self.reference_window)
        self._values = deque(maxlen=self.window)
        self._memory = deque([0.0] * self.persistence, maxlen=self.persistence)
        self._learned = self._learn(self._seed) if self._seed and not self.adaptive else ()
        self._result = CorrelationResult()
        self._history = deque(maxlen=self.reference_window)
        self._profiles = {}
        self._anchors = {}
        self._relations = {}
        self._updates = 0
        self._received = -1
        self._pending = deque()
        if self.adaptive:
            for row in self._seed:
                self._update_adaptive(row)
            self._result = CorrelationResult()

    def fit(self, reference: Iterable[Iterable[float]]):
        """Seed optional past history; fixed mode treats it as a frozen reference."""
        rows, width = deque(maxlen=self.reference_window), None
        for value in reference:
            row = self._row(value, width)
            width = len(row)
            rows.append(row)
        minimum = self.min_samples if self.adaptive else self.window
        if len(rows) < minimum:
            raise ValueError(f'reference requires at least {minimum} observations')
        learned = () if self.adaptive else self._learn(rows)
        self._seed = tuple(rows)
        self.reset()
        self._learned = learned
        return self

    def update(self, observation: Iterable[float]) -> CorrelationResult:
        """Append one aligned vector; reject invalid input without state changes."""
        row = self._row(observation, self._width)
        if self.adaptive:
            self._width = len(row)
            self._received += 1
            self._pending.append(row)
            if len(self._pending) <= self.alignment_tolerance:
                self._result = CorrelationResult(provisional=True,
                    observed_index=self._received,alignment_delay=self.alignment_tolerance,pending=True)
                return self._result
            target = self._pending.popleft()
            result = self._update_adaptive(target,tuple(self._pending))
            self._result = replace(result,score_index=self._received-self.alignment_tolerance,
                observed_index=self._received,alignment_delay=self.alignment_tolerance)
            return self._result
        if not self.reference_ready:
            reference = [*self._reference, row]
            learned = self._learn(reference) if len(reference) == self.reference_window else ()
            self._width = len(row)
            self._reference.append(row)
            self._learned = learned
            return self._result
        rows = [*self._values, row][-self.window:]
        if len(rows) < self.window:
            self._values.append(row)
            return self._result
        values, pairs = np.asarray(rows, dtype=float), []
        affected = set()
        for i, j, k, baseline in self._learned:
            profile, constants = self._profile(values, i, j)
            r, ref = profile[k], baseline[k]
            both_constant = all(constants[k])
            change = 0.0 if both_constant else abs(ref) if r is None else abs(r - ref)
            severity = min(1.0, max(0.0, (change - self.min_change) / (1 - self.min_change)))
            if severity >= self.alert_threshold:
                affected.update((i, j))
            pairs.append(CorrelationPairResult((i, j), k-self.max_lag, ref, r,
                         change, severity, profile, baseline, both_constant))
        raw_score = max(p.score for p in pairs)
        memory = [*self._memory, raw_score][-self.persistence:]
        score = math.fsum(memory) / self.persistence
        self._values.append(row)
        self._memory.append(raw_score)
        self._result = CorrelationResult(True, score, raw_score,
                                        score >= self.alert_threshold,
                                        tuple(pairs), tuple(sorted(affected)))
        return self._result

    def _anchor(self, profile, available_lag=None):
        candidates = [k for k, r in enumerate(profile)
                      if r is not None and abs(r) >= self.min_correlation
                      and (available_lag is None or abs(k-self.max_lag) <= available_lag)]
        if not candidates:
            return None
        peak = max(abs(profile[k]) for k in candidates)
        return min((k for k in candidates if abs(profile[k]) >= peak - .02),
                   key=lambda k: (abs(k-self.max_lag), -abs(profile[k])))

    @staticmethod
    def _deviation(values):
        """Representable population std, retaining small variation on a DC level."""
        with np.errstate(over='ignore', invalid='ignore'):
            shifted = values - values[0]
        outer = 1.0
        if not np.isfinite(shifted).all():
            outer = float(np.max(np.abs(values)))
            shifted = values / outer
        unit = float(np.max(np.abs(shifted)))
        if unit == 0:
            return 0.0
        normalized = shifted / unit
        normalized -= normalized.mean()
        return outer * (unit * math.sqrt(float(normalized @ normalized) / len(values)))

    def _pair_severity(self,x,y,ref,floors,ready):
        cx,cy=self._center(x,floors[0]),self._center(y,floors[1])
        both_flat=cx is None and cy is None
        r=None if cx is None or cy is None else max(-1.,min(1.,float(cx @ cy)))
        change=0. if both_flat else abs(ref) if r is None else abs(r-ref)
        severity=0.
        if ready and not both_flat:
            severity=min(1.,max(0.,(change-self.min_change)/(1-self.min_change)))
            ratios=[self._deviation(v)/f if f>0 else math.inf for v,f in ((x,floors[0]),(y,floors[1]))]
            evidence=max(ratios) if r is None else min(ratios)
            severity*=min(1.,max(0.,(evidence-1.)/2))
        return r,change,severity,both_flat

    def _aligned_pair(self,context,end,lag,i,j,ref,floors,ready):
        """Best coherent window match within +/- tolerance around the learned lag.

        Each emitted point can choose a different offset; all samples within
        its window share that offset, avoiding arbitrary noise-fitting per sample.
        Candidate offsets use the same x indices and the same sample count.
        """
        radius=self.alignment_tolerance
        start=max(0,end-self.window+1)
        left=max(start,start-lag,radius-lag)
        right=min(end,end-lag,len(context)-1-lag-radius)
        if right-left+1<3:
            return None
        indices=np.arange(left,right+1)
        x=context[indices,i]
        candidates=[]
        for shift in range(-radius,radius+1):
            y=context[indices+lag+shift,j]
            measured=self._pair_severity(x,y,ref,floors,ready)
            candidates.append((measured,shift,y))
        measured,shift,y=min(candidates,key=lambda c:(c[0][2],c[0][1],abs(c[1]),c[1]))
        return measured,shift,x,y

    @staticmethod
    def _lagged(values,i,j,lag):
        if lag>0:
            return values[:-lag,i],values[lag:,j]
        if lag<0:
            return values[-lag:,i],values[:lag,j]
        return values[:,i],values[:,j]

    def _relation(self,x,y):
        """Symmetric normalized affine model of the previously accepted relation."""
        cx,cy=self._center(x,0.),self._center(y,0.)
        if cx is None or cy is None:
            return None
        sx,sy=self._deviation(x),self._deviation(y)
        if sx<=0 or sy<=0:
            return None
        return (math.fsum(float(v)/len(x) for v in x),sx,
                math.fsum(float(v)/len(y) for v in y),sy,
                max(-1.,min(1.,float(cx @ cy))))

    @staticmethod
    def _relative_error(x,y,model):
        """RMS of both directional errors / their expected CURRENT levels.

        std is a fallback around zero, where percent-of-level is undefined.
        Clipping at 200% keeps finite extreme inputs and reversed signs bounded.
        """
        mx,sx,my,sy,r=model
        with np.errstate(over='ignore',invalid='ignore',divide='ignore',under='ignore'):
            expected_y=my+r*((x-mx)/sx)*sy
            expected_x=mx+r*((y-my)/sy)*sx
            def errors(actual,expected,scale):
                level=np.maximum(np.abs(expected),scale)
                unit=np.maximum(np.maximum(np.abs(actual),np.abs(expected)),level)
                relative=np.abs(actual/unit-expected/unit)/(level/unit)
                return np.clip(np.nan_to_num(relative,nan=2.,posinf=2.,neginf=2.),0.,2.)
            ex,ey=errors(x,expected_x,sx),errors(y,expected_y,sy)
        return math.sqrt(float(np.mean(ex*ex+ey*ey))/2)

    def _relative_weight(self,x,y,model):
        if model is None or self.relative_change_scale==0:
            return None,1.
        error=self._relative_error(x,y,model)
        return error,error/(error+self.relative_change_scale)

    def _update_adaptive(self, row, future=()):
        rows = [*self._history, row][-self.window:]
        n, width = len(rows), len(row)
        previous = np.asarray(self._history, dtype=float)
        floors = tuple(max(self.noise_floor, self.relative_noise_floor *
                           self._deviation(previous[:, i])) if len(previous) > 1
                       else self.noise_floor for i in range(width))
        values = np.asarray(rows, dtype=float)
        context=np.asarray([*self._history,row,*future],dtype=float)
        end=len(self._history)
        zero_floors = (0.0,) * width
        ready, pairs, next_profiles, affected = n >= self.min_samples, [], {}, set()
        next_relations={}
        # Startup averaging avoids treating the first tiny CCF estimate as a
        # permanently healthy reference; thereafter the effective horizon is bounded.
        alpha = max(2 / (self.reference_window + 1),
                    1 / max(1, self._updates - self.min_samples + 3))
        for i in range(width):
            for j in range(i + 1, width):
                key = (i, j)
                baseline = self._profiles.get(key, (None,) * (2*self.max_lag+1))
                profile, constants = self._profile(values, i, j, floors)
                raw_profile, _ = self._profile(values, i, j, zero_floors)
                k = self._anchors.get(key)
                monitored = k is not None
                r, ref, change, severity, uninformative = None, None, 0.0, 0.0, True
                alignment_shift=0
                strict_r,strict_severity=None,0.
                aligned_raw=raw_profile[k] if monitored else None
                relative_error,relative_weight,correlation_score=None,1.,0.
                unaligned_score=0.
                if monitored:
                    ref=baseline[k]
                    x,y=self._lagged(values,i,j,k-self.max_lag)
                    r,change,severity,uninformative=self._pair_severity(x,y,ref,(floors[i],floors[j]),ready)
                    strict_r,strict_severity=r,severity
                    model=self._relations.get(key)
                    _,strict_relative_weight=self._relative_weight(x,y,model)
                    unaligned_score=strict_severity*strict_relative_weight
                    if self.alignment_tolerance and len(future)>=self.alignment_tolerance:
                        aligned=self._aligned_pair(context,end,k-self.max_lag,i,j,ref,
                                                   (floors[i],floors[j]),ready)
                        if aligned is not None:
                            (r,change,severity,uninformative),alignment_shift,x,y=aligned
                            ax,ay=self._center(x,0.),self._center(y,0.)
                            aligned_raw=None if ax is None or ay is None else max(-1.,min(1.,float(ax @ ay)))
                    correlation_score=severity
                    relative_error,relative_weight=self._relative_weight(x,y,model)
                    severity*=relative_weight
                    if severity >= self.alert_threshold:
                        affected.update(key)
                pairs.append(CorrelationPairResult(key, k-self.max_lag if monitored else None,
                    ref, r, change, severity, raw_profile, baseline, uninformative, monitored,
                    raw_profile[k] if monitored else None,alignment_shift,unaligned_score,
                    aligned_raw,relative_error,relative_weight,correlation_score))
                # Lookahead is only for the delayed score. Training and its gate
                # depend solely on data through the target, never its right halo.
                trusted = (not monitored or (strict_severity < self.alert_threshold and strict_r is not None
                           and abs(strict_r) >= self.min_correlation and strict_r*ref > 0))
                if n >= self.min_samples - 1 and trusted:
                    next_profiles[key] = tuple(
                        old if current is None else current if old is None else
                        current if not monitored else max(-1.0, min(1.0, old + alpha*(current-old)))
                        for old, current in zip(baseline, profile))
                    if monitored:
                        past_x,past_y=self._lagged(values,i,j,k-self.max_lag)
                        relation=self._relation(past_x,past_y)
                        if relation is not None:
                            old=self._relations.get(key)
                            next_relations[key]=relation if old is None else tuple(
                                (1-alpha)*a+alpha*b for a,b in zip(old,relation))
        raw_score = max((p.score for p in pairs), default=0.0)
        memory = [*self._memory, raw_score][-self.persistence:]
        score = math.fsum(memory) / self.persistence if ready else 0.0
        result = CorrelationResult(ready, score, raw_score, ready and score >= self.alert_threshold,
            tuple(pairs), tuple(sorted(affected)), n < self.window, floors)
        # All diagnostics above use only the prior reference. Learn after scoring.
        self._width = width
        self._history.append(row)
        self._profiles.update(next_profiles)
        self._relations.update(next_relations)
        for key, profile in next_profiles.items():
            if key not in self._anchors:
                # Tiny lagged overlaps can have a spurious perfect CCF. Start
                # with zero lag; acquire a nonzero delay only on a full window.
                anchor = self._anchor(profile, 0 if n < self.window else self.max_lag)
                if anchor is not None and self._interaction_support(values, *key, anchor):
                    self._anchors[key] = anchor
                    a,b=self._lagged(values,*key,anchor-self.max_lag)
                    relation=self._relation(a,b)
                    if relation is not None:
                        self._relations[key]=relation
        self._updates += 1
        self._memory.append(raw_score)
        self._result = result
        return result

    def _interaction_support(self, values, i, j, k):
        """A single common edge cannot establish a persistent relationship.

        Require variation to be spread across several increments in BOTH
        aligned series, instead of one dominant transition out of a flat state.
        This is a startup heuristic, not an effective-sample-size p-value.
        """
        lag = k-self.max_lag
        x,y = (values[:-lag,i],values[lag:,j]) if lag>0 else (
            (values[-lag:,i],values[:lag,j]) if lag<0 else (values[:,i],values[:,j]))
        if len(values)<min(self.window,2*self.min_samples) or len(x)<6:
            return False
        # Confirm the same signed relation on two disjoint segments. A strong
        # coefficient in a tiny prefix must not freeze a spurious baseline.
        midpoint=len(x)//2
        signs=[]
        for a,b in ((0,midpoint),(midpoint,len(x))):
            cx,cy=self._center(x[a:b],0.),self._center(y[a:b],0.)
            if cx is None or cy is None:
                return False
            correlation=float(cx @ cy)
            if abs(correlation)<self.min_correlation:
                return False
            signs.append(correlation>0)
        if signs[0]!=signs[1]:
            return False
        for segment in (x,y):
            centered = self._center(segment,0.0)
            if centered is None:
                return False
            delta = np.diff(centered)
            unit = float(np.max(np.abs(delta)))
            if unit == 0:
                return False
            squares = (delta/unit)**2
            participation = float(squares.sum())**2 / float(squares @ squares)
            if participation + 1e-12 < min(3.0,len(delta)/2):
                return False
        return True

    def _fresh(self):
        detector = type(self)(self.window, self.max_lag,
            reference_window=self.reference_window, min_correlation=self.min_correlation,
            min_change=self.min_change, persistence=self.persistence,
            alert_threshold=self.alert_threshold, noise_floor=self.noise_floor,
            adaptive=self.adaptive, min_samples=self.min_samples,
            relative_noise_floor=self.relative_noise_floor,
            alignment_tolerance=self.alignment_tolerance,
            relative_change_scale=self.relative_change_scale,
            logger_name=self.logger.name,
            logger_props={'level': logging.getLevelName(self.logger.level)})
        detector._seed = self._seed
        detector.reset()
        return detector

    def replay(self, data: Iterable[Iterable[float]]) -> list[CorrelationResult]:
        """Causal fresh replay, preserving the active stream and explicit fit."""
        detector = self._fresh()
        return [detector.update(row) for row in data]

    def detect_series(self, data: Iterable[Iterable[float]]) -> list[float]:
        return [r.score for r in self.replay(data)]

    def analyze(self, data: Iterable[Iterable[float]]) -> CorrelationResult:
        detector = self._fresh()
        for row in data:
            detector.update(row)
        return detector.result

    def detect(self, data: Iterable[Iterable[float]]) -> float:
        return self.analyze(data).score

    def score(self, data: Iterable[Iterable[float]]) -> float:
        return self.detect(data)



@dataclass(frozen=True, slots=True)
class PredictionIntervalResult:
    """Interval issued BEFORE this observation, and its bounded severity.

    Warmup has ready=False, no bounds and score=0. scale is the multiplier
    used for these bounds; the detector's scale may already target the next
    observation. Bounds include both endpoints. raw_score is distance-only;
    score also includes score_factor for interval stability. interval_change
    measures normalized bound movement after admitting this observation.
    Neither score is a probability.
    """

    ready: bool = False
    infimum: float | None = None
    supremum: float | None = None
    score: float = 0.0
    scale: float = 1.0
    raw_score: float = 0.0
    interval_change: float = 0.0
    score_factor: float = 1.0


class PredictionIntervalDetector(Detector):
    """Predict the next scalar from exactly the preceding ``window`` values.

    Base interval: mean +/- t_(1-alpha/2, n-1) * s * sqrt(1 + 1/n), with
    sample std (ddof=1), alpha=1-confidence and a positive half-width floor.
    At scale=1 this is a pointwise prediction interval for IID normal data,
    not a confidence interval for the mean or a simultaneous stream guarantee.
    Trend, serial dependence and heavy tails can invalidate nominal coverage.

    ``scale`` multiplies the half-width around the mean. fit() can widen it
    using walk-forward training residuals. Optional auto_scale adjusts its
    logarithm AFTER scoring: rate * (miss - alpha), bounded below by the
    manual scale and above by max_scale. Frequent misses therefore widen
    subsequent intervals; sustained hits gradually undo that widening.

    When admitting the observation barely moves the interval, severity is
    multiplied by stable_score_factor (default 0.1). Movement compares both
    windows at the SAME current scale, normalized by their larger half-width.
    The scored bounds still use only preceding data. The current observation
    qualifies its severity through the resulting window change; no future
    observation is used. Raw distance and coverage/adaptation stay separate.

    Every finite observation, including an outlier, enters the rolling window
    only after scoring. Unrepresentable finite bounds raise ValueError without
    advancing state. One instance holds one series. Batch methods replay an
    independent stream, preserving only explicit fit() history/calibration.
    """

    def __init__(self, window: int = 30, confidence: float = 0.95, *,
                 scale: float = 1.0, auto_scale: bool = False,
                 adaptation_rate: float = 0.05, max_scale: float = 100.0,
                 noise_floor: float = 1e-9,
                 stability_threshold: float = 0.3, stable_score_factor: float = 0.1,
                 logger_name: str | None = None,
                 logger_props: dict[str, str] | None = None):
        if isinstance(window, bool) or not isinstance(window, Integral) or window < 2:
            raise ValueError('window must be an integer >= 2')
        for name, value in (('confidence', confidence), ('scale', scale),
                            ('adaptation_rate', adaptation_rate),
                            ('max_scale', max_scale), ('noise_floor', noise_floor),
                            ('stability_threshold', stability_threshold),
                            ('stable_score_factor', stable_score_factor)):
            self._number(name, value)
        if not 0 < confidence < 1:
            raise ValueError('confidence must be in (0, 1)')
        if scale <= 0 or max_scale < scale:
            raise ValueError('require 0 < scale <= max_scale')
        if not 0 < adaptation_rate <= 1:
            raise ValueError('adaptation_rate must be in (0, 1]')
        if noise_floor <= 0:
            raise ValueError('noise_floor must be positive')
        if not 0 <= stability_threshold < 1:
            raise ValueError('stability_threshold must be in [0, 1)')
        if not 0 < stable_score_factor <= 1:
            raise ValueError('stable_score_factor must be in (0, 1]')
        if not isinstance(auto_scale, bool):
            raise ValueError('auto_scale must be a bool')
        super().__init__(logger_name, logger_props)
        self.window, self.confidence = int(window), float(confidence)
        self.initial_scale, self.max_scale = float(scale), float(max_scale)
        self.auto_scale, self.adaptation_rate = auto_scale, float(adaptation_rate)
        self.noise_floor = float(noise_floor)
        self.stability_threshold = float(stability_threshold)
        self.stable_score_factor = float(stable_score_factor)
        # isf avoids rounding (1 + confidence) / 2 up to exactly 1.
        self._critical = float(student_t.isf((1 - self.confidence) / 2,
                                            self.window - 1)) * math.sqrt(1 + 1 / self.window)
        if not math.isfinite(self._critical):
            raise ValueError('confidence/window produce an unrepresentable t quantile')
        self._seed: tuple[float, ...] = ()
        self._fitted_scale = self.initial_scale
        self.reset()

    @staticmethod
    def _number(name, value):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError(f'{name} must be a finite number')

    @property
    def ready(self) -> bool:
        """Whether a forecast for the NEXT observation is available."""
        return len(self._history) == self.window

    @property
    def result(self) -> PredictionIntervalResult:
        """Latest scored interval; result.ready distinguishes warmup from zero."""
        return self._result

    @property
    def scale(self) -> float:
        """Current multiplier for the next forecast, including online changes."""
        return self._scale

    @property
    def fitted_scale(self) -> float:
        """Multiplier retained by reset() and fresh batch replays."""
        return self._fitted_scale

    def reset(self):
        """Restore fitted history/scale; discard subsequent stream/adaptation."""
        self._history: deque[float] = deque(self._seed, maxlen=self.window)
        self._scale = self._fitted_scale
        self._result = PredictionIntervalResult(scale=self._scale)

    def _base_prediction(self, history):
        try:
            center = mean(history)
            # ulp also keeps a constant, very large level's bounds distinct.
            half_width = max(self.noise_floor, math.ulp(center),
                             self._critical * stdev(history))
        except ArithmeticError as error:
            raise ValueError('prediction statistics must be representable') from error
        if not math.isfinite(half_width):
            raise ValueError('prediction half-width must be finite')
        return center, half_width

    @staticmethod
    def _bounds(center, base_half_width, scale):
        half_width = base_half_width * scale
        infimum, supremum = center - half_width, center + half_width
        if (half_width <= 0 or not math.isfinite(infimum)
                or not math.isfinite(supremum) or infimum >= supremum):
            raise ValueError('scaled prediction bounds must be finite and distinct')
        return infimum, supremum, half_width

    def predict(self) -> tuple[float, float] | None:
        """Bounds for the next point, without consuming data; None in warmup."""
        if not self.ready:
            return None
        center, half_width = self._base_prediction(self._history)
        return self._bounds(center, half_width, self._scale)[:2]

    def fit(self, reference: Iterable[float], *, calibrate: bool = True):
        """Seed the last window and optionally widen on training-only errors.

        Each of the len(reference)-window residuals uses only its previous
        window. Their nearest-rank confidence quantile sets a multiplier,
        clipped to [initial_scale, max_scale]. With exactly window values
        there are no calibration residuals. This is empirical calibration,
        not a distribution-free guarantee. Validation is atomic.
        """
        if not isinstance(calibrate, bool):
            raise ValueError('calibrate must be a bool')
        values = []
        for value in reference:
            self._number('reference value', value)
            values.append(float(value))
        if len(values) < self.window:
            raise ValueError('reference requires at least window values')
        fitted_scale = self.initial_scale
        if calibrate and len(values) > self.window:
            history = deque(values[:self.window], maxlen=self.window)
            ratios = []
            for value in values[self.window:]:
                center, half_width = self._base_prediction(history)
                ratios.append(abs(value - center) / half_width)
                history.append(value)
            ratios.sort()
            quantile = ratios[math.ceil(self.confidence * len(ratios)) - 1]
            fitted_scale = min(self.max_scale, max(self.initial_scale, quantile))
        seed = tuple(values[-self.window:])
        self._bounds(*self._base_prediction(seed), fitted_scale)
        self._seed, self._fitted_scale = seed, fitted_scale
        self.reset()
        return self

    def _interval_change(self, value, center, base_half_width):
        """Bound movement after admission, without mutating history or scale.

        For centers mu/mu_next and base half-widths h/h_next this equals
        max(abs(inf_next-inf), abs(sup_next-sup)) / max(scale*h, scale*h_next),
        capped at 1. The identity max(abs(a-b), abs(a+b)) = abs(a)+abs(b)
        avoids forming potentially overflowing bounds. Unrepresentable next
        statistics count as a large change, preserving the valid current score.
        """
        next_history = [*self._history][1:] + [value]
        try:
            next_center, next_half_width = self._base_prediction(next_history)
        except ValueError:
            return 1.0
        unit = max(base_half_width, next_half_width)
        center_change = (abs(next_center - center) / unit) / self._scale
        width_change = abs(next_half_width - base_half_width) / unit
        return min(1.0, center_change + width_change)

    def update(self, value: float) -> PredictionIntervalResult:
        """Score against preceding bounds, then learn the observation.

        Inside [infimum, supremum], score=0. Outside, let d be distance to
        the nearest boundary and h the scaled half-width: raw_score=d/(h+d).
        score = raw_score * stable_score_factor when normalized interval
        movement <= stability_threshold; otherwise score = raw_score.
        Clamp floating-point saturation to nextafter(1, 0), so 1 is never
        returned, even when the distance or its normalized ratio overflows.
        """
        self._number('value', value)
        value = float(value)
        if not self.ready:
            self._history.append(value)
            self._result = PredictionIntervalResult(scale=self._scale)
            return self._result
        center, base_half_width = self._base_prediction(self._history)
        infimum, supremum, half_width = self._bounds(center, base_half_width, self._scale)
        miss = value < infimum or value > supremum
        score = 0.0
        if miss:
            distance = infimum - value if value < infimum else value - supremum
            relative = distance / half_width
            score = (relative / (1 + relative) if math.isfinite(relative) else 1.0)
            score = max(math.nextafter(0.0, 1.0), min(math.nextafter(1.0, 0.0), score))
        raw_score = score
        change = self._interval_change(value, center, base_half_width)
        factor = self.stable_score_factor if change <= self.stability_threshold else 1.0
        if miss:
            score = max(math.nextafter(0.0, 1.0), raw_score * factor)
        result = PredictionIntervalResult(True, infimum, supremum, score, self._scale,
                                          raw_score=raw_score, interval_change=change,
                                          score_factor=factor)
        next_scale = self._scale
        if self.auto_scale:
            adjustment = self.adaptation_rate * (float(miss) - (1 - self.confidence))
            next_scale = min(self.max_scale,
                             max(self.initial_scale, self._scale * math.exp(adjustment)))
        self._history.append(value)
        self._scale, self._result = next_scale, result
        return result

    def _fresh(self):
        detector = type(self)(self.window, self.confidence,
                              scale=self.initial_scale, auto_scale=self.auto_scale,
                              adaptation_rate=self.adaptation_rate, max_scale=self.max_scale,
                              noise_floor=self.noise_floor, logger_name=self.logger.name,
                              stability_threshold=self.stability_threshold,
                              stable_score_factor=self.stable_score_factor,
                              logger_props={'level': logging.getLevelName(self.logger.level)})
        detector._seed, detector._fitted_scale = self._seed, self._fitted_scale
        detector.reset()
        return detector

    def predict_intervals(self, data: Iterable[float]) -> list[PredictionIntervalResult]:
        """Fresh causal replay with bounds and severity for EVERY input point."""
        detector = self._fresh()
        return [detector.update(value) for value in data]

    def detect_single_seria(self, data: Iterable[float]) -> list[float]:
        """Score-only replay, matching the existing detectors' plotting API."""
        return [result.score for result in self.predict_intervals(data)]

    def analyze(self, data: Iterable[float]) -> PredictionIntervalResult:
        """Final diagnostics of a fresh replay, without active-stream mutation."""
        detector = self._fresh()
        for value in data:
            detector.update(value)
        return detector.result

    def detect(self, data: Iterable[float]) -> float:
        """Final replay score, or zero for empty/warmup input."""
        return self.analyze(data).score

    def score(self, data: Iterable[float]) -> float:
        return self.detect(data)


# Compatibility for imports of the former placeholder.
BondDetector = PredictionIntervalDetector
