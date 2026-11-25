import json
import math
from pathlib import Path

import numpy as np
import pytest
from atropos_ui.detector_runtime import DETECTORS

CusumDetector = DETECTORS['CUSUM']


def small_large_saw():
    values = np.tile([100., 95., 100., 100., 100.], 240)
    for first, last in ((350, 500), (750, 850)):
        values[first:last] = np.tile([100., 50., 100., 100., 100.], (last-first)//5)
    return values


def test_known_small_saw_is_background_but_larger_saw_has_substantial_response():
    values = small_large_saw()
    detector = CusumDetector(drift_mode='adaptive')
    scores = np.array([detector.update(float(v)) for v in values])
    assert detector.threshold == 5
    assert 0 < scores[200:300].max() < .1
    assert scores[350:400].max() > .8
    assert scores[750:790].max() > .8
    assert scores[1100:].max() < .1
    assert len(detector._response_memory) <= detector.inertia_window
    assert len(detector._reference) <= detector.reference_window


def test_new_saw_grows_then_fades_as_previous_background_adapts():
    rng = np.random.default_rng(43)
    values = 100 + rng.normal(0, .4, 1000)
    values[300:800] += 100
    for first in range(350, 650, 6):
        values[first:first+4] += [0, -4, -9, -3]
    scores = np.array(CusumDetector(drift_mode='adaptive').detect_single_seria(values))
    assert scores[350:400].max() > .2
    assert scores[600:650].max() < .1
    assert scores[600:650].max() < .4 * scores[350:400].max()
    assert scores[430:480].mean() < scores[350:400].max()
    assert scores[250:300].max() < .01


def test_adaptive_drift_uses_only_previous_deltas_and_requires_both_directions():
    normal = np.tile([100., 95., 100., 100., 100.], 30)
    detector = CusumDetector(drift_mode='adaptive').fit(normal)
    assert detector.drift_allowance == detector.drift_std_fraction * detector.noise_std
    allowance = detector.drift_allowance
    before = list(detector._reference)
    detector.update(100.)
    detector.update(-1000.)
    assert list(detector._reference) == before
    assert detector.drift_allowance == allowance
    assert max(CusumDetector(drift_mode='adaptive').fit([100.] * 101).detect_single_seria(
        [100.] * 20 + [50.] * 100)) == 0


def test_adaptive_stream_batch_prefix_and_scaling_agree():
    values = small_large_saw()
    instance = CusumDetector(drift_mode='adaptive')
    streamed = [instance.update(float(v)) for v in values]
    assert streamed == CusumDetector(drift_mode='adaptive').detect_single_seria(values)
    assert streamed[:600] == CusumDetector(drift_mode='adaptive').detect_single_seria(values[:600])
    np.testing.assert_allclose(streamed, CusumDetector(drift_mode='adaptive').detect_single_seria(values * 100))
    np.testing.assert_allclose(streamed, CusumDetector(drift_mode='adaptive').detect_single_seria(-values))


@pytest.mark.parametrize('std', [2., 8.5])
def test_stationary_noise_does_not_become_a_large_anomaly(std):
    for seed in (11, 22, 33, 43, 55):
        values = np.random.default_rng(seed).normal(15., std, 1000)
        scores = CusumDetector(drift_mode='adaptive').detect_single_seria(values)
        assert all(math.isfinite(v) and 0 <= v <= 1 for v in scores)
        assert np.mean(np.array(scores[200:]) >= .5) < .01


@pytest.mark.parametrize('kwargs', [{'drift_mode': 'unknown'}, {'drift_quantile': 0},
                                  {'drift_quantile': 1}, {'drift_quantile': float('nan')},
                                  {'drift_std_fraction': 0}, {'drift_std_fraction': float('inf')}])
def test_invalid_adaptive_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        CusumDetector(**kwargs)


def test_real_three_hour_cpu_has_weak_cycle_response_and_stronger_irregular_bursts():
    path = Path(__file__).resolve().parents[2] / 'detectors/tests/fixtures/cusum-cpu-20261122-3h-15s.json'
    fixture = json.loads(path.read_text(encoding='utf-8'))
    values = fixture['values']
    assert fixture['step'] == 15 and len(values) == 720
    detector = CusumDetector(drift_mode='adaptive')
    scores = np.array(detector.detect_single_seria(values))
    ordinary = scores[100:200]
    assert .02 < ordinary.mean() < .2
    assert scores[200:320].max() > ordinary.max()
    assert scores.max() > 2 * ordinary.mean()
    assert scores.max() < .5
    assert detector.threshold == 5
    # Appending the late rising section must not change historical decisions.
    assert list(scores[:480]) == detector.detect_single_seria(values[:480])
