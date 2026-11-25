"""Causal, bounded-window robust trend detector. No third-party dependencies."""
from collections import deque
from dataclasses import dataclass
import math
from statistics import median


@dataclass(frozen=True, slots=True)
class TrendConfig:
    window_seconds: float
    variance_threshold: float
    min_tangent: float
    value_scale: float = 1.0
    min_samples: int = 8
    min_coverage: float = 0.9
    noise_floor: float = 1e-6
    huber_delta: float = 1.5
    iterations: int = 12
    step_ratio: float = 0.5
    confirmation: int = 1
    max_samples: int = 4096

    def __post_init__(self):
        for name in ('window_seconds', 'value_scale', 'noise_floor', 'huber_delta'):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        for name in ('variance_threshold', 'min_tangent'):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and nonnegative')
        if not 0 < self.min_coverage <= 1 or not 0 <= self.step_ratio < 1:
            raise ValueError('invalid coverage or step_ratio')
        for name in ('min_samples', 'iterations', 'confirmation', 'max_samples'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f'{name} must be a positive integer')
        if self.min_samples < 4 or self.max_samples < self.min_samples:
            raise ValueError('require 4 <= min_samples <= max_samples')


@dataclass(frozen=True, slots=True)
class TrendResult:
    timestamp: float
    ready: bool
    detected: bool
    direction: str
    tangent: float
    residual_variance: float
    step_like: bool
    samples: int
    reason: str


def _line(x, y, weights):
    total = sum(weights)
    mx = sum(w * t for w, t in zip(weights, x)) / total
    my = sum(w * v for w, v in zip(weights, y)) / total
    xx = sum(w * (t - mx) ** 2 for w, t in zip(weights, x))
    slope = sum(w * (t - mx) * (v - my)
                for w, t, v in zip(weights, x, y)) / xx
    return my - slope * mx, slope


def _fit(x, y, cfg):
    # Median of separated-pair slopes supplies an outlier-resistant start.
    gap = len(x) // 2
    slope = median((y[i + gap] - y[i]) / (x[i + gap] - x[i])
                   for i in range(len(x) - gap))
    intercept = median(v - slope * t for t, v in zip(x, y))
    for _ in range(cfg.iterations):
        residuals = [v - intercept - slope * t for t, v in zip(x, y)]
        center = median(residuals)
        sigma = max(cfg.noise_floor,
                    1.4826 * median(abs(r - center) for r in residuals))
        cutoff = cfg.huber_delta * sigma
        weights = [min(1.0, cutoff / max(abs(r), cfg.noise_floor)) for r in residuals]
        new_intercept, new_slope = _line(x, y, weights)
        change = abs(new_intercept - intercept) + abs(new_slope - slope) * (x[-1] - x[0])
        intercept, slope = new_intercept, new_slope
        if change <= cfg.noise_floor * 0.01:
            break
    residuals = [v - intercept - slope * t for t, v in zip(x, y)]
    center = median(residuals)
    sigma = max(cfg.noise_floor, 1.4826 * median(abs(r - center) for r in residuals))
    # Winsorized residual second moment: isolated spikes cannot dominate it.
    cap = cfg.huber_delta * sigma
    variance = sum(min(r * r, cap * cap) for r in residuals) / len(y)
    cleaned = [intercept + slope * t + max(-cap, min(cap, r))
               for t, r in zip(x, residuals)]
    return intercept, slope, variance, cleaned


def _step_like(x, cleaned, cfg):
    if cfg.step_ratio == 0:
        return False
    # Compare a line and a two-level step on the SAME winsorized data.
    # Prefix sums make the split search O(n). This is a heuristic, not a p-value.
    origin = cleaned[0]
    y = [v - origin for v in cleaned]
    a, b = _line(x, y, [1.0] * len(y))
    line_sse = sum((v - a - b * t) ** 2 for t, v in zip(x, y))
    if line_sse <= len(y) * cfg.noise_floor ** 2:
        return False
    total, total2 = sum(y), sum(v * v for v in y)
    left = left2 = 0.0
    best = math.inf
    margin = max(2, len(y) // 5)
    for k, v in enumerate(y, 1):
        left += v
        left2 += v * v
        if margin <= k <= len(y) - margin:
            right_n = len(y) - k
            sse = left2 - left * left / k
            sse += total2 - left2 - (total - left) ** 2 / right_n
            best = min(best, max(0.0, sse))
    return best < cfg.step_ratio * line_sse


class RuntimeTrendDetector:
    """One instance per series; timestamps in seconds, tangent in scaled units/min.

    update() stores samples in O(1) amortized time. evaluate() costs
    O(iterations * n * log(n)) with Python's sorting-based median and may be
    called once a minute independently of a 15-second scrape interval.
    Repeated evaluation without new data does not advance confirmation.
    """

    def __init__(self, config: TrendConfig):
        self.config = config
        self._points = deque(maxlen=config.max_samples)
        self._last_result = None
        self._last_evaluation = None
        self._streak = 0
        self._direction = 0

    def update(self, timestamp: float, value: float) -> None:
        if not math.isfinite(timestamp) or not math.isfinite(value):
            raise ValueError('timestamp and value must be finite')
        if self._points and timestamp <= self._points[-1][0]:
            raise ValueError('timestamps must be strictly increasing')
        if self._points and timestamp - self._points[-1][0] > self.config.window_seconds:
            self._streak = self._direction = 0
        self._points.append((timestamp, value))
        boundary = timestamp - self.config.window_seconds
        while self._points and self._points[0][0] < boundary:
            self._points.popleft()

    def evaluate(self) -> TrendResult:
        if not self._points:
            raise ValueError('update must be called before evaluate')
        now = self._points[-1][0]
        if self._last_evaluation == now:
            return self._last_result
        cfg = self.config
        ready = (len(self._points) >= cfg.min_samples and
                 now - self._points[0][0] >= cfg.min_coverage * cfg.window_seconds)
        slope = variance = 0.0
        step = False
        reason = 'warmup'
        direction = 0
        if ready:
            x = [(t - now) / 60.0 for t, _ in self._points]
            y = [v / cfg.value_scale for _, v in self._points]
            _, slope, variance, cleaned = _fit(x, y, cfg)
            step = _step_like(x, cleaned, cfg)
            if step:
                reason = 'step'
            elif variance > cfg.variance_threshold:
                reason = 'variance'
            elif abs(slope) <= cfg.min_tangent:
                reason = 'flat'
            else:
                reason = 'trend'
                direction = 1 if slope > 0 else -1
        if direction:
            self._streak = self._streak + 1 if direction == self._direction else 1
        else:
            self._streak = 0
        self._direction = direction
        detected = direction != 0 and self._streak >= cfg.confirmation
        if direction and not detected:
            reason = 'confirmation'
        result = TrendResult(now, ready, detected,
                             'up' if direction > 0 else 'down' if direction < 0 else 'none',
                             slope, variance, step, len(self._points), reason)
        self._last_result, self._last_evaluation = result, now
        return result
