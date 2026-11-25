"""Frozen healthy-cycle removal experiment before the existing MadDetector.

The period and phase template are learned only from an initial prefix. The
model is never updated with later data. Training residuals are diagnostic;
both MAD streams start after training for a fair, causal comparison.
"""

from dataclasses import asdict, dataclass
import numpy as np
from scipy.optimize import minimize_scalar

from apps.correlation import fft_periods
from apps.detectors import MadDetector
from tests.trend_replay_appendices import MSK


TRAIN_SAMPLES = 720
MAD_CONFIG = {'window': 20, 'data_max_size': 100}


def finite_array(values):
    data = np.asarray(values, dtype=float)
    if data.ndim != 1 or not len(data) or not np.isfinite(data).all():
        raise ValueError('Expected a nonempty finite one-dimensional series')
    return data


@dataclass(frozen=True)
class HealthyCycle:
    period_samples: float
    baseline_ms: float
    phase_points: tuple[float, ...]
    cycle_values_ms: tuple[float, ...]
    training_samples: int
    harmonics_for_period: int
    training_r2: float

    def cycle_at(self, indices):
        positions = finite_array(indices)
        return np.interp((positions/self.period_samples) % 1,
                         self.phase_points, self.cycle_values_ms, period=1)

    def predict(self, indices):
        return self.baseline_ms+self.cycle_at(indices)

    def residual(self, values, start=0):
        data = finite_array(values)
        if isinstance(start, bool) or not isinstance(start, (int, np.integer)) or start < 0:
            raise ValueError('start must be a nonnegative sample index')
        return data-self.predict(np.arange(len(data))+start)


def fit_healthy_cycle(reference, *, phase_bins=64, harmonics=6):
    """Estimate a fractional period and robust folded template from healthy data.

    The existing FFT/sine period finder nominates a fundamental. A six-harmonic
    fit refines its period inside +/-2 samples using the lowest 90% squared
    residuals. Phase-bin medians then estimate the waveform. Fractional phase
    locations and circular interpolation support noninteger sampling periods.
    """
    data = finite_array(reference)
    if len(data) < 128:
        raise ValueError('At least 128 healthy samples are required')
    for name, value, minimum in (('phase_bins', phase_bins, 8), ('harmonics', harmonics, 1)):
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < minimum:
            raise ValueError(f'{name} must be an integer >= {minimum}')
    periods = fft_periods(data, min_period=12, min_cycles=4,
                          min_power_fraction=.15, max_cycles=1)
    if not periods:
        raise ValueError('No sufficiently strong repeated cycle in the healthy prefix')
    center = float(np.median(data))
    y = data-center
    indices = np.arange(len(data))
    def design(period):
        phase = 2*np.pi*indices/period
        return np.column_stack([np.ones(len(data))]+
                               [f(h*phase) for h in range(1, harmonics+1) for f in (np.sin, np.cos)])
    def loss(period):
        matrix = design(period)
        coefficients = np.linalg.lstsq(matrix, y, rcond=None)[0]
        errors = y-matrix@coefficients
        return float(np.mean(np.sort(errors*errors)[:int(.9*len(data))]))
    guess = periods[0]
    lower, upper = max(12, guess-2), min(len(data)/4, guess+2)
    if lower >= upper:
        raise ValueError('Healthy prefix is too short for the nominated cycle')
    fit = minimize_scalar(loss, bounds=(lower, upper), method='bounded', options={'xatol': 1e-7})
    choices = (float(guess), lower, upper, float(fit.x))
    period = min(choices, key=loss)
    phase = (indices/period) % 1
    groups = np.minimum(phase_bins-1, (phase*phase_bins).astype(int))
    occupied = sorted(set(groups))
    phase_points = np.asarray([np.median(phase[groups == group]) for group in occupied])
    profile = np.asarray([np.median(y[groups == group]) for group in occupied])
    # Subtract only the fixed healthy level and periodic waveform. Future level,
    # amplitude, period and noise changes therefore remain in the residual.
    gaps = np.diff(np.r_[phase_points, phase_points[0]+1])
    mean_cycle = float(np.sum(.5*(profile+np.roll(profile, -1))*gaps))
    cycle = profile-mean_cycle
    baseline = center+mean_cycle
    predicted = baseline+np.interp(phase, phase_points, cycle, period=1)
    energy = float(np.sum((data-data.mean())**2))
    r2 = 1-float(np.sum((data-predicted)**2))/energy
    return HealthyCycle(float(period), baseline, tuple(float(v) for v in phase_points),
                        tuple(float(v) for v in cycle), len(data), harmonics, r2)


def replay_without_cycle(case, train_samples=TRAIN_SAMPLES):
    if train_samples < 128 or train_samples+MAD_CONFIG['data_max_size'] > len(case.values):
        raise ValueError('Need healthy training data and a separate complete MAD window')
    model = fit_healthy_cycle(case.values[:train_samples])
    predicted = model.predict(np.arange(len(case.values)))
    residuals = case.values-predicted
    raw_detector, residual_detector = MadDetector(**MAD_CONFIG), MadDetector(**MAD_CONFIG)
    rows = []
    for index, (raw, residual, stamp) in enumerate(zip(case.values, residuals, case.timestamps)):
        if index < train_samples:
            raw_score = clean_score = None
            phase, ready = 'cycle_training', False
        else:
            raw_score, clean_score = raw_detector.update(float(raw)), residual_detector.update(float(residual))
            ready = residual_detector.ready
            phase = 'scored' if ready else 'mad_warmup'
        rows.append({'index': index, 'timestamp_utc': stamp.isoformat().replace('+00:00', 'Z'),
                     'timestamp_msk': stamp.astimezone(MSK).isoformat(), 'raw_value_ms': float(raw),
                     'healthy_prediction_ms': float(predicted[index]), 'residual_ms': float(residual),
                     'cycle_training': index < train_samples, 'phase': phase, 'ready': ready,
                     'raw_score': raw_score, 'residual_score': clean_score,
                     'raw_local_mad_ms': raw_detector.local_mad, 'raw_reference_mad_ms': raw_detector.reference_mad,
                     'residual_local_mad_ms': residual_detector.local_mad,
                     'residual_reference_mad_ms': residual_detector.reference_mad})
    return rows, model


def describe_comparison(rows, model):
    selected = [r for r in rows if r['ready']]
    segments = []
    for label, start, stop in (('03:24:45–05:00', 819, 1200), ('05:00–10:00', 1200, 2400),
                               ('10:00–11:50', 2400, len(rows))):
        part = [r for r in rows[start:stop] if r['ready']]
        if not part:
            continue
        raw = np.asarray([r['raw_value_ms'] for r in part])
        clean = np.asarray([r['residual_ms'] for r in part])
        segments.append({'interval_msk': label, 'samples': len(part),
                         'raw_mean_score': float(np.mean([r['raw_score'] for r in part])),
                         'residual_mean_score': float(np.mean([r['residual_score'] for r in part])),
                         'raw_max_score': max(r['raw_score'] for r in part),
                         'residual_max_score': max(r['residual_score'] for r in part),
                         'raw_std_ms': float(np.std(raw)), 'residual_std_ms': float(np.std(clean))})
    peak = max(selected, key=lambda r: r['residual_score'])
    return {'model': asdict(model), 'period_minutes': model.period_samples*.25,
            'first_ready_msk': selected[0]['timestamp_msk'], 'ready_samples': len(selected),
            'raw_max_score': max(r['raw_score'] for r in selected),
            'residual_max_score': peak['residual_score'], 'residual_peak_msk': peak['timestamp_msk'],
            'segments': segments}
