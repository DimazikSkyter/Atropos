import math
import unittest
from dataclasses import FrozenInstanceError

import numpy as np

from apps.detectors import (
    BondDetector, Detector, PredictionIntervalDetector, PredictionIntervalResult,
)


def snapshot(detector):
    return (tuple(detector._history), detector.scale, detector.fitted_scale,
            detector.result, detector._seed)


class PredictionIntervalTests(unittest.TestCase):
    def test_renamed_detector_inherits_detector_and_keeps_import_alias(self):
        detector = PredictionIntervalDetector(logger_name='prediction-test',
                                              logger_props={'level': 'WARNING'})
        self.assertIsInstance(detector, Detector)
        self.assertIs(BondDetector, PredictionIntervalDetector)
        self.assertEqual(detector.logger.name, 'prediction-test')
        self.assertEqual(detector.logger.level, 30)

    def test_two_point_cauchy_prediction_has_closed_form_bounds(self):
        # df=1 Student is Cauchy: its 75% quantile is exactly 1.
        # For [1, 3], sample variance=2; h=sqrt(2)*sqrt(1+1/2).
        detector = PredictionIntervalDetector(window=2, confidence=0.5).fit([1, 3])
        lower, upper = detector.predict()
        self.assertAlmostEqual(lower, 2 - math.sqrt(3), places=10)
        self.assertAlmostEqual(upper, 2 + math.sqrt(3), places=10)

    def test_small_sample_prediction_uses_sample_variance_and_not_mean_ci(self):
        # Tabulated t_(0.975, 4), independent of the implementation's isf call.
        values = [2, 4, 5, 7, 12]
        detector = PredictionIntervalDetector(window=5).fit(values)
        lower, upper = detector.predict()
        variance = sum((x - 6) ** 2 for x in values) / 4
        half_width = 2.7764451051977987 * math.sqrt(variance * 1.2)
        self.assertAlmostEqual(lower, 6 - half_width, places=9)
        self.assertAlmostEqual(upper, 6 + half_width, places=9)
        mean_ci_half_width = 2.7764451051977987 * math.sqrt(variance / 5)
        self.assertGreater((upper - lower) / 2, 2 * mean_ci_half_width)

    def test_every_scored_window_uses_exactly_the_preceding_window(self):
        data = [1, 3, 100, -2, 5, 8]
        results = PredictionIntervalDetector(2, 0.5).predict_intervals(data)
        for i in range(2, len(data)):
            a, b = data[i - 2:i]
            center = (a + b) / 2
            h = abs(a - b) * math.sqrt(3) / 2
            self.assertAlmostEqual(results[i].infimum, center - h, places=8)
            self.assertAlmostEqual(results[i].supremum, center + h, places=8)

    def test_current_outlier_cannot_expand_its_own_interval(self):
        fitted = [0, 1, -1, 2, -2]
        quiet = PredictionIntervalDetector(5).fit(fitted).update(0)
        spike = PredictionIntervalDetector(5).fit(fitted).update(1e6)
        self.assertEqual((quiet.infimum, quiet.supremum), (spike.infimum, spike.supremum))
        self.assertEqual(quiet.score, 0)
        self.assertGreater(spike.score, 0.999)

    def test_warmup_and_one_result_for_each_input_including_last(self):
        detector = PredictionIntervalDetector(3)
        self.assertIsNone(detector.predict())
        for value in (1, 2, 3):
            result = detector.update(value)
            self.assertFalse(result.ready)
            self.assertIsNone(result.infimum)
            self.assertIsNone(result.supremum)
            self.assertEqual(result.score, 0)
        self.assertTrue(detector.ready)
        bounds = detector.predict()
        result = detector.update(100)
        self.assertTrue(result.ready)
        self.assertEqual(bounds, (result.infimum, result.supremum))
        self.assertGreater(result.score, 0)
        replay = PredictionIntervalDetector(3).predict_intervals([1, 2, 3, 100])
        self.assertEqual(len(replay), 4)
        self.assertEqual(replay[-1], result)

    def test_raw_score_zero_inside_inclusive_boundaries_and_monotone_symmetric_outside(self):
        # A constant history and floor=2 give the exact interval [-2, 2].
        def evaluate(value):
            return PredictionIntervalDetector(3, noise_floor=2).fit([0]*3).update(value).raw_score

        for value in (-2, -1, 0, 1, 2):
            self.assertEqual(evaluate(value), 0)
        expected = {3: 1/3, 4: 1/2, 6: 2/3, 20: 0.9}
        for value, score in expected.items():
            self.assertAlmostEqual(evaluate(value), score)
            self.assertEqual(evaluate(value), evaluate(-value))
        self.assertTrue(all(evaluate(a) < evaluate(b) for a, b in zip(expected, list(expected)[1:])))

    def test_score_never_reaches_one_even_at_extreme_distances(self):
        for value in (-1e308, -1e30, 1e30, 1e308):
            result = PredictionIntervalDetector(3).fit([0]*3).update(value)
            self.assertTrue(math.isfinite(result.score))
            self.assertGreater(result.score, 0.99)
            self.assertLess(result.score, 1)

    def test_score_handles_overflow_in_distance_to_boundary(self):
        detector = PredictionIntervalDetector(3).fit([1e308]*3)
        result = detector.update(-1e308)
        self.assertEqual(result.score, math.nextafter(1, 0))

    def test_score_is_positive_for_an_outside_point_even_if_normalization_underflows(self):
        detector = PredictionIntervalDetector(3, noise_floor=5e307).fit([5e307]*3)
        self.assertEqual(detector.predict()[0], 0)
        result = detector.update(-math.nextafter(0, 1))
        self.assertGreater(result.score, 0)
        self.assertLess(result.score, 1)

    def test_manual_scale_and_variance_change_interval_width(self):
        def bounds(values, scale=1):
            return PredictionIntervalDetector(5, scale=scale).fit(values).predict()

        lower, upper = bounds([-2, -1, 0, 1, 2])
        lower2, upper2 = bounds([-2, -1, 0, 1, 2], scale=2)
        self.assertAlmostEqual(lower2, 2*lower)
        self.assertAlmostEqual(upper2, 2*upper)
        lower3, upper3 = bounds([-6, -3, 0, 3, 6])
        self.assertAlmostEqual(upper3 - lower3, 3*(upper - lower))

    def test_higher_confidence_widens_bounds(self):
        widths = []
        for confidence in (0.5, 0.9, 0.95, 0.99):
            lo, hi = PredictionIntervalDetector(5, confidence).fit(range(5)).predict()
            widths.append(hi - lo)
        self.assertEqual(widths, sorted(set(widths)))

    def test_constant_history_stays_finite_and_uses_floor(self):
        for level in (0, 100, -5, 1e308):
            detector = PredictionIntervalDetector(5).fit([level]*5)
            result = detector.update(level)
            self.assertEqual(result.score, 0)
            self.assertTrue(result.infimum < level < result.supremum)
            self.assertTrue(math.isfinite(result.infimum) and math.isfinite(result.supremum))
        detector = PredictionIntervalDetector(5, noise_floor=0.1).fit([10]*5)
        np.testing.assert_allclose(detector.predict(), (9.9, 10.1))

    def test_auto_scale_reacts_only_after_scoring_and_can_relax(self):
        detector = PredictionIntervalDetector(5, auto_scale=True).fit([-2, -1, 0, 1, 2])
        forecast = detector.predict()
        result = detector.update(100)
        self.assertEqual(forecast, (result.infimum, result.supremum))
        self.assertEqual(result.scale, 1)
        self.assertGreater(detector.scale, result.scale)
        wider_scale = detector.scale
        result = detector.update(20)
        self.assertEqual(result.score, 0)
        self.assertEqual(result.scale, wider_scale)
        self.assertLess(detector.scale, wider_scale)
        self.assertGreaterEqual(detector.scale, detector.initial_scale)

    def test_auto_scale_enforces_minimum_and_maximum(self):
        detector = PredictionIntervalDetector(5, scale=0.2, max_scale=0.3,
                                              auto_scale=True, adaptation_rate=1)
        for value in range(200):
            detector.update(value)
            self.assertTrue(0.2 <= detector.scale <= 0.3)
        self.assertEqual(detector.scale, 0.3)
        for _ in range(500):
            detector.update(0)
        self.assertEqual(detector.scale, 0.2)

    def test_disabled_auto_scale_is_constant_despite_repeated_misses(self):
        detector = PredictionIntervalDetector(20, scale=0.2)
        results = [detector.update(value) for value in range(500)]
        self.assertTrue(all(r.score > 0 for r in results[20:]))
        self.assertEqual(detector.scale, 0.2)

    def test_auto_scale_improves_held_out_coverage_of_underwide_intervals(self):
        detector = PredictionIntervalDetector(20, scale=0.2, auto_scale=True)
        results = [detector.update(value) for value in range(1600)]
        self.assertLess(np.mean([r.score == 0 for r in results[20:40]]), 0.1)
        coverage = np.mean([r.score == 0 for r in results[400:]])
        self.assertGreater(coverage, 0.93)
        self.assertLess(coverage, 0.98)
        self.assertGreater(detector.scale, 0.2)

    def test_fit_calibration_widens_and_generalizes_to_following_trend_points(self):
        calibrated = PredictionIntervalDetector(20, scale=0.2).fit(range(300))
        raw = PredictionIntervalDetector(20, scale=0.2).fit(range(300), calibrate=False)
        self.assertGreater(calibrated.fitted_scale, raw.fitted_scale)
        self.assertGreater(raw.update(300).score, 0)
        # Equality to the calibrated boundary can differ by one float ulp.
        self.assertLess(calibrated.update(300).score, 1e-12)
        self.assertTrue(all(r.score < 1e-12 for r in calibrated.predict_intervals(range(300, 600))))

    def test_fit_calibration_uses_walk_forward_errors_and_quantile(self):
        # df=1, 50% PI: history [a,b] -> center=(a+b)/2, h=sqrt(3)*abs(a-b)/2.
        values = [0, 2, 4, 2, 10, 0, 3]
        ratios = []
        for i in range(2, len(values)):
            a, b = values[i-2:i]
            ratios.append(abs(values[i]-(a+b)/2) / (math.sqrt(3)*abs(a-b)/2))
        expected = sorted(ratios)[2]
        detector = PredictionIntervalDetector(2, 0.5, scale=0.1).fit(values)
        self.assertAlmostEqual(detector.fitted_scale, expected, places=10)
        self.assertEqual(tuple(detector._history), (0, 3))

    def test_calibration_respects_cap_and_never_narrows_manual_scale(self):
        capped = PredictionIntervalDetector(3, scale=0.01, max_scale=0.1).fit(range(100))
        self.assertEqual(capped.fitted_scale, 0.1)
        manual = PredictionIntervalDetector(3, scale=5).fit(range(100))
        self.assertEqual(manual.fitted_scale, 5)
        minimal = PredictionIntervalDetector(3, scale=2).fit([1, 2, 3])
        self.assertEqual(minimal.fitted_scale, 2)
        self.assertTrue(minimal.ready)

    def test_iid_normal_coverage_matches_nominal_on_independent_trials(self):
        # Independent windows avoid treating overlapping-window trials as IID.
        # At .95 and 5000 trials, binomial SE is about .0031.
        for window in (5, 30):
            rng = np.random.default_rng(43 + window)
            trials = rng.normal(size=(5000, window + 1))
            covered = 0
            detector = PredictionIntervalDetector(window)
            for row in trials:
                result = detector.fit(row[:-1], calibrate=False).update(row[-1])
                covered += result.score == 0
            self.assertTrue(0.935 < covered / len(trials) < 0.965, covered / len(trials))

    def test_batch_stream_scores_and_final_diagnostics_agree(self):
        reference = list(range(60))
        data = [*range(60, 100), -10, 120]
        stream = PredictionIntervalDetector(10, auto_scale=True).fit(reference)
        expected = [stream.update(v) for v in data]
        batch = PredictionIntervalDetector(10, auto_scale=True).fit(reference)
        self.assertEqual(batch.predict_intervals(iter(data)), expected)
        self.assertEqual(batch.detect_single_seria(iter(data)), [r.score for r in expected])
        self.assertEqual(batch.analyze(iter(data)), expected[-1])
        self.assertEqual(batch.detect(data), expected[-1].score)
        self.assertEqual(batch.score(data), expected[-1].score)
        self.assertEqual(batch.predict_intervals([]), [])
        self.assertEqual(batch.detect([]), 0)
        self.assertFalse(batch.analyze([]).ready)

    def test_future_samples_do_not_change_past_bounds_or_scores_with_auto_scale(self):
        detector = PredictionIntervalDetector(5, auto_scale=True)
        prefix = [*range(20), -100, 10]
        expected = detector.predict_intervals(prefix)
        self.assertEqual(detector.predict_intervals([*prefix, 1e6, -1e6])[:len(prefix)], expected)

    def test_replays_and_predict_do_not_mutate_stream_and_results_are_immutable(self):
        detector = PredictionIntervalDetector(5, auto_scale=True).fit(range(30))
        detector.update(-100)
        before = snapshot(detector)
        detector.predict()
        detector.predict_intervals(range(100))
        detector.analyze(range(100))
        detector.detect_single_seria(range(100))
        self.assertEqual(snapshot(detector), before)
        with self.assertRaises(FrozenInstanceError):
            detector.result.score = 1

    def test_reset_restores_fit_seed_and_scale_and_refit_replaces_them(self):
        detector = PredictionIntervalDetector(5, scale=0.2, auto_scale=True).fit(range(30))
        baseline = snapshot(detector)
        detector.update(-100)
        detector.reset()
        self.assertEqual(snapshot(detector), baseline)
        detector.fit([100]*5)
        self.assertEqual(tuple(detector._history), (100,)*5)
        self.assertEqual(detector.scale, 0.2)
        fresh = PredictionIntervalDetector(5)
        fresh.update(0)
        fresh.reset()
        self.assertFalse(fresh.ready)
        self.assertEqual(fresh.result, PredictionIntervalResult())

    def test_history_memory_is_bounded(self):
        detector = PredictionIntervalDetector(5, auto_scale=True)
        for value in range(1000):
            detector.update(value)
        self.assertEqual(tuple(detector._history), (995, 996, 997, 998, 999))

    def test_translation_scale_and_sign_transform_bounds_and_preserve_scores(self):
        reference = [-2, -1, 0, 1, 2]
        data = [3, -10, 2, 1, -20]
        original = PredictionIntervalDetector(5).fit(reference).predict_intervals(data)
        for multiplier, offset in ((2, 100), (-3, 20), (1e-3, 0), (1e100, 0)):
            changed = PredictionIntervalDetector(5).fit(
                [v*multiplier+offset for v in reference]).predict_intervals(
                    [v*multiplier+offset for v in data])
            for a, b in zip(original, changed):
                self.assertAlmostEqual(a.score, b.score, places=12)
                expected = sorted((a.infimum*multiplier+offset, a.supremum*multiplier+offset))
                np.testing.assert_allclose([b.infimum, b.supremum], expected, rtol=1e-12)

    def test_invalid_constructor_arguments(self):
        cases = ({'window': 1}, {'window': True}, {'window': 2.5},
                 {'confidence': 0}, {'confidence': 1}, {'confidence': math.nan},
                 {'confidence': True}, {'scale': 0}, {'scale': -1},
                 {'scale': math.inf}, {'max_scale': 0.5}, {'max_scale': math.nan},
                 {'auto_scale': 1}, {'adaptation_rate': 0}, {'adaptation_rate': 2},
                 {'noise_floor': 0}, {'noise_floor': math.inf}, {'scale': '2'},
                 {'stability_threshold': -0.1}, {'stability_threshold': 1},
                 {'stability_threshold': math.nan}, {'stability_threshold': True},
                 {'stable_score_factor': 0}, {'stable_score_factor': 1.1},
                 {'stable_score_factor': math.inf}, {'stable_score_factor': False})
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PredictionIntervalDetector(**kwargs)

    def test_invalid_update_and_fit_do_not_mutate_active_state(self):
        detector = PredictionIntervalDetector(5, auto_scale=True).fit(range(20))
        detector.update(100)
        before = snapshot(detector)
        for value in (math.nan, math.inf, -math.inf, True, '100', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                detector.update(value)
            self.assertEqual(snapshot(detector), before)
            with self.assertRaises(ValueError):
                detector.fit([0]*5 + [value])
            self.assertEqual(snapshot(detector), before)
        for reference in ([], [1, 2]):
            with self.assertRaises(ValueError):
                detector.fit(reference)
            self.assertEqual(snapshot(detector), before)
        with self.assertRaises(ValueError):
            detector.fit(range(10), calibrate=1)
        self.assertEqual(snapshot(detector), before)

    def test_unrepresentable_bounds_fail_clearly_and_atomically(self):
        detector = PredictionIntervalDetector(2).fit([0, 0])
        before = snapshot(detector)
        with self.assertRaises(ValueError):
            detector.fit([1e308, -1e308])
        self.assertEqual(snapshot(detector), before)
        # The huge value can be scored against valid past bounds, but its
        # admission makes the NEXT prediction unrepresentable.
        detector.update(1e308)
        before = snapshot(detector)
        with self.assertRaises(ValueError):
            detector.update(0)
        self.assertEqual(snapshot(detector), before)


if __name__ == '__main__':
    unittest.main()
