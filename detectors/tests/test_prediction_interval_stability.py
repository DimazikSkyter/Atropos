import math
import unittest

import numpy as np

from apps.detectors import PredictionIntervalDetector
from tests.acf_scenarios import persistence_scenarios
from tests.prediction_interval_scenarios import stability_scenarios


class PredictionIntervalStabilityTests(unittest.TestCase):
    def test_identical_window_bounds_reduce_score_exactly_tenfold(self):
        history = [0, 1]*15
        detector = PredictionIntervalDetector(scale=0.1).fit(history, calibrate=False)
        forecast = detector.predict()
        result = detector.update(0)  # replaces 0: mean and variance unchanged
        self.assertEqual((result.infimum, result.supremum), forecast)
        self.assertEqual(detector.predict(), forecast)
        self.assertEqual(result.interval_change, 0)
        self.assertGreater(result.raw_score, 0.5)
        self.assertEqual(result.score_factor, 0.1)
        self.assertAlmostEqual(result.score, result.raw_score/10, places=15)

    def test_small_bound_movement_is_also_attenuated(self):
        detector = PredictionIntervalDetector(noise_floor=10).fit([0]*30)
        result = detector.update(11)
        self.assertTrue(0 < result.interval_change < detector.stability_threshold)
        self.assertAlmostEqual(result.interval_change, (11/30)/10)
        self.assertAlmostEqual(result.raw_score, 1/11)
        self.assertAlmostEqual(result.score, 1/110)

    def test_large_bound_change_preserves_first_spike_without_future_data(self):
        detector = PredictionIntervalDetector(noise_floor=10).fit([0]*30)
        forecast = detector.predict()
        result = detector.update(100)
        self.assertEqual((result.infimum, result.supremum), forecast)
        self.assertGreater(result.interval_change, detector.stability_threshold)
        self.assertEqual(result.score_factor, 1)
        self.assertAlmostEqual(result.score, 0.9)
        self.assertEqual(result.score, result.raw_score)

    def test_scale_adaptation_does_not_itself_count_as_window_movement(self):
        detector = PredictionIntervalDetector(scale=0.1, auto_scale=True).fit([0, 1]*15,
                                                                            calibrate=False)
        old_scale = detector.scale
        for value in [0, 1]*10:
            result = detector.update(value)
            self.assertEqual(result.interval_change, 0)
            self.assertEqual(result.score_factor, 0.1)
        self.assertGreater(detector.scale, old_scale)

    def test_attenuation_does_not_change_forecasts_fit_or_miss_based_adaptation(self):
        rng = np.random.default_rng(11)
        reference, data = rng.normal(size=100), rng.normal(size=400)
        muted = PredictionIntervalDetector(auto_scale=True).fit(reference)
        raw = PredictionIntervalDetector(auto_scale=True, stable_score_factor=1).fit(reference)
        self.assertEqual(muted.fitted_scale, raw.fitted_scale)
        for value in data:
            a, b = muted.update(value), raw.update(value)
            self.assertEqual((a.infimum, a.supremum, a.scale), (b.infimum, b.supremum, b.scale))
            self.assertEqual(a.raw_score, b.score)
            self.assertEqual(a.score == 0, b.score == 0)
            self.assertEqual(muted.scale, raw.scale)

    def test_threshold_and_factor_are_configurable(self):
        def score(threshold, factor):
            return PredictionIntervalDetector(noise_floor=10, stability_threshold=threshold,
                                               stable_score_factor=factor).fit([0]*30).update(11)
        strict, loose = score(0.01, 0.1), score(0.1, 0.1)
        self.assertEqual(strict.score, strict.raw_score)
        self.assertAlmostEqual(loose.score, strict.score/10)
        self.assertAlmostEqual(score(0.1, 0.25).score, strict.score/4)
        self.assertEqual(score(0.1, 1).score, strict.score)

    def test_both_location_and_width_changes_can_preserve_severity(self):
        for history, value in (([0]*30, 100), ([-1, 1]*15, 100)):
            result = PredictionIntervalDetector().fit(history).update(value)
            self.assertGreater(result.interval_change, 0.3)
            self.assertEqual(result.score_factor, 1)

    def test_diagnostics_are_bounded_and_weighted_scores_stay_positive_outside(self):
        rng = np.random.default_rng(55)
        values = np.r_[rng.normal(size=300), [100], rng.normal(size=300)]
        for result in PredictionIntervalDetector().predict_intervals(values):
            self.assertTrue(0 <= result.interval_change <= 1)
            self.assertTrue(0 <= result.score <= result.raw_score < 1)
            if result.ready:
                self.assertIn(result.score_factor, (0.1, 1))
                self.assertEqual(result.raw_score == 0, result.score == 0)

    def test_snapshot_noise_before_and_after_new_cycle_is_tenfold_weaker(self):
        scenario = next(s for s in persistence_scenarios() if s[0] == 'periodic_new')
        _, _, _, values, start, end = scenario
        for auto_scale in (False, True):
            with self.subTest(auto_scale=auto_scale):
                results = PredictionIntervalDetector(auto_scale=auto_scale).predict_intervals(values)
                for section in (results[30:start], results[end+30:]):
                    misses = [r for r in section if r.raw_score > 0]
                    self.assertGreater(len(misses), 10)
                    for result in misses:
                        self.assertEqual(result.score_factor, 0.1)
                        self.assertAlmostEqual(result.score, result.raw_score/10)
                self.assertGreater(results[start].score, 0.8)
                self.assertEqual(results[start].score, results[start].raw_score)

    def test_high_mean_and_ma8_weak_cycles_reduce_background_across_seeds(self):
        for seed in (11, 43, 55):
            for scenario in stability_scenarios(seed):
                with self.subTest(scenario=scenario.name):
                    results = PredictionIntervalDetector().predict_intervals(scenario.values)[30:]
                    misses = [r for r in results if r.raw_score > 0]
                    self.assertGreater(len(misses), 20)
                    attenuated = sum(r.score_factor == 0.1 for r in misses)
                    self.assertGreater(attenuated/len(misses), 0.9)
                    self.assertLess(sum(r.score for r in misses)/sum(r.raw_score for r in misses), 0.2)

    def test_stability_is_invariant_to_level_units_and_sign(self):
        rng = np.random.default_rng(43)
        values = rng.normal(0, 0.05, 300) + 0.02*np.sin(np.arange(300))
        expected = PredictionIntervalDetector().predict_intervals(values)[30:]
        for scale, offset in ((1, 1000), (100, 0), (-2, 20)):
            actual = PredictionIntervalDetector().predict_intervals(scale*values+offset)[30:]
            np.testing.assert_allclose([r.interval_change for r in actual],
                                       [r.interval_change for r in expected], atol=1e-10)
            np.testing.assert_allclose([r.score for r in actual], [r.score for r in expected], atol=1e-10)

    def test_unrepresentable_next_window_preserves_valid_current_extreme_score(self):
        result = PredictionIntervalDetector(2).fit([0, 0]).update(1e308)
        self.assertEqual(result.interval_change, 1)
        self.assertEqual(result.score_factor, 1)
        self.assertEqual(result.score, math.nextafter(1, 0))

    def test_batch_and_reset_retain_custom_stability_settings(self):
        detector = PredictionIntervalDetector(scale=0.1, stable_score_factor=0.25,
                                               stability_threshold=0.4).fit([0, 1]*15,
                                                                             calibrate=False)
        result = detector.update(0)
        self.assertEqual(result.score_factor, 0.25)
        self.assertEqual(detector.analyze([0]), result)
        detector.reset()
        self.assertEqual(detector.update(0), result)


if __name__ == '__main__':
    unittest.main()
