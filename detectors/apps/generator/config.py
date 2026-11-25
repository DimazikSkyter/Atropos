"""All amplitudes and standard deviations are percentages of abs(mean)."""

from dataclasses import dataclass
import math
from numbers import Integral, Real


def finite_number(name: str, value: Real) -> None:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')


def integer(name: str, value: int, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')


@dataclass(frozen=True, slots=True)
class NoiseConfig:
    """Independent Gaussian + stationary AR(1) Gaussian + Student-t noise.

    Each std_percent controls the component's population standard deviation.
    Setting all three to zero disables noise. No component is a random walk.
    """

    white_std_percent: float = 0.35
    correlated_std_percent: float = 0.4
    correlation: float = 0.65
    heavy_tail_std_percent: float = 0.1
    degrees_of_freedom: float = 5.0

    def __post_init__(self):
        for name in ('white_std_percent', 'correlated_std_percent', 'heavy_tail_std_percent'):
            value = getattr(self, name)
            finite_number(name, value)
            if value < 0:
                raise ValueError(f'{name} must be nonnegative')
            object.__setattr__(self, name, float(value))
        finite_number('correlation', self.correlation)
        if not -1 < self.correlation < 1:
            raise ValueError('correlation must be strictly between -1 and 1')
        object.__setattr__(self, 'correlation', float(self.correlation))
        finite_number('degrees_of_freedom', self.degrees_of_freedom)
        if self.degrees_of_freedom <= 2:
            raise ValueError('degrees_of_freedom must be > 2 for finite variance')
        object.__setattr__(self, 'degrees_of_freedom', float(self.degrees_of_freedom))


@dataclass(frozen=True, slots=True)
class CycleConfig:
    """A sampled sine wave; amplitude_percent is peak amplitude, not peak-to-peak."""

    period_points: int
    amplitude_percent: float
    phase_radians: float = 0.0

    def __post_init__(self):
        integer('period_points', self.period_points, 2)
        finite_number('amplitude_percent', self.amplitude_percent)
        if self.amplitude_percent < 0:
            raise ValueError('amplitude_percent must be nonnegative')
        finite_number('phase_radians', self.phase_radians)
        object.__setattr__(self, 'period_points', int(self.period_points))
        object.__setattr__(self, 'amplitude_percent', float(self.amplitude_percent))
        object.__setattr__(self, 'phase_radians', float(self.phase_radians))


@dataclass(frozen=True, slots=True)
class SpikeConfig:
    """Rare positive bursts, separated by at least one unaffected point.

    probability is checked at each eligible point. One event lasts 1..max_points
    and adds at most 50% of abs(mean). Zero probability disables bursts.
    """

    probability: float = 0.001
    min_amplitude_percent: float = 1.0
    max_amplitude_percent: float = 8.0
    max_points: int = 3

    def __post_init__(self):
        finite_number('probability', self.probability)
        if not 0 <= self.probability <= 1:
            raise ValueError('probability must be between 0 and 1')
        for name in ('min_amplitude_percent', 'max_amplitude_percent'):
            finite_number(name, getattr(self, name))
        if not 0 <= self.min_amplitude_percent <= self.max_amplitude_percent <= 50:
            raise ValueError('Spike amplitudes must satisfy 0 <= min <= max <= 50 percent')
        integer('max_points', self.max_points, 1)
        if self.max_points > 3:
            raise ValueError('Background spikes can last at most 3 points')
        object.__setattr__(self, 'probability', float(self.probability))
        object.__setattr__(self, 'min_amplitude_percent', float(self.min_amplitude_percent))
        object.__setattr__(self, 'max_amplitude_percent', float(self.max_amplitude_percent))
        object.__setattr__(self, 'max_points', int(self.max_points))
