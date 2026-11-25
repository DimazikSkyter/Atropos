import json
from pathlib import Path

import numpy as np
import pytest
from atropos_ui.detector_runtime import DETECTORS

Trend = DETECTORS['Trend']


def cpu_values():
    fixture = Path(__file__).resolve().parents[2] / 'detectors/tests/fixtures/cusum-cpu-20261122-3h-15s.json'
    return json.loads(fixture.read_text(encoding='utf-8'))['values']


def test_real_cpu_growth_is_visible_without_reclassifying_earlier_cycles():
    values = cpu_values()
    detector = Trend()
    rows = [detector.update(v) for v in values]
    scores = np.array([r.score for r in rows])
    assert not rows[238].ready and rows[239].ready
    assert scores[:680].max() < .1
    assert scores[680:].max() > .2
    assert scores[-1] > .2 and rows[-1].direction == 1
    # This is visible preliminary evidence; the stricter bend check is retained.
    assert not any(r.detected for r in rows)
    assert list(scores[:480]) == Trend().detect_single_seria(values[:480])
    np.testing.assert_allclose(scores, Trend(value_scale=100).detect_single_seria(np.array(values)*100))
    assert max(Trend(evidence_mode='residual').detect_single_seria(values)) == 0


def test_hac_errors_match_independent_dense_covariance():
    # Dense OLS covariance oracle, independent of streaming moment bookkeeping.
    detector = Trend(window=20, data_max_size=120)
    rng = np.random.default_rng(97)
    for i, value in enumerate(rng.normal(0, 2, 140)):
        detector.update(float(100 + .1*i + value))
    window = detector._reference
    y = np.array(window.values)
    n = len(y)
    x = np.arange(n) - (n-1)/2
    design = np.column_stack((np.ones(n), x))
    inverse = np.linalg.inv(design.T @ design)
    residual = y-design @ np.linalg.lstsq(design, y, rcond=None)[0]
    bandwidth = max(1, min(n//4, int(np.sqrt(n))))
    distance = np.abs(np.arange(n)[:,None]-np.arange(n)[None,:])
    kernel = np.maximum(0, 1-distance/(bandwidth+1))
    meat = design.T @ (residual[:,None] * kernel * residual[None,:]) @ design
    covariance = inverse @ meat @ inverse * n/(n-2)
    slope_error, projected_error = window.uncertainty(60)
    assert slope_error == pytest.approx(np.sqrt(covariance[1,1]), rel=1e-9)
    vector = np.array([1,60])
    assert projected_error == pytest.approx(np.sqrt(vector@covariance@vector), rel=1e-9)


def test_unknown_trend_evidence_mode_is_rejected():
    with pytest.raises(ValueError):
        Trend(evidence_mode='unknown')
