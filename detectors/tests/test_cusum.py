import math
import unittest

import numpy as np

from apps.detectors import CusumDetector
from tests.cusum_scenarios import legacy_scenarios, noisy_scenario, saw_scenario


class CusumTests(unittest.TestCase):
    def test_original_bursts_and_shallow_dip_have_distinct_severity(self):
        for name, values, start, end in legacy_scenarios():
            with self.subTest(scenario=name):
                scores = np.asarray(CusumDetector().detect_single_seria(values))
                self.assertEqual(len(scores), len(values))
                self.assertTrue(np.all(np.isfinite(scores)))
                self.assertTrue(np.all((scores >= 0) & (scores <= 1)))
                if name == 'shallow':
                    self.assertLessEqual(scores.max(), 0.2)
                else:
                    self.assertGreater(scores[start:end + 9].max(), 0.5)
                    alarm = start + np.flatnonzero(scores[start:end + 9] > 0.5)[0]
                    self.assertGreaterEqual(alarm, start)
                    self.assertLess(alarm, end)
                self.assertLess(scores[250:start - 20].max(), 0.05)
                self.assertLess(scores[end + 20:900].max(), 0.05)

    def test_saw_on_quiet_background_stays_detected(self):
        scores = np.asarray(CusumDetector().detect_single_seria(saw_scenario()))
        self.assertLess(scores[350:360].max(), 0.2)
        self.assertGreater(np.mean(scores[500:650] > 0.5), 0.95)
        self.assertGreater(scores[500:650].mean(), 0.8)
        self.assertLess(scores[200:300].max(), 0.05)
        self.assertLess(scores[680:780].max(), 0.05)
        self.assertLess(scores[300:310].max(), 0.05)  # Single level step.

    def test_same_saw_on_noisy_background_has_lower_score(self):
        for seed in (11, 22, 33, 43, 55):
            with self.subTest(seed=seed):
                quiet = np.asarray(CusumDetector().detect_single_seria(saw_scenario(seed=seed)))
                noisy = np.asarray(CusumDetector().detect_single_seria(noisy_scenario(seed)))
                self.assertGreater(quiet[500:650].mean(), 0.8)
                self.assertLess(noisy[360:650].mean(), 0.1)
                self.assertLess(np.mean(noisy[360:650] > 0.5), 0.05)

    def test_white_noise_does_not_become_a_burst(self):
        for seed in (11, 22, 33, 43, 55):
            for sigma in (0.1, 1.0, 10.0):
                with self.subTest(seed=seed, sigma=sigma):
                    values = np.random.default_rng(seed).normal(100, sigma, 1000)
                    scores = np.asarray(CusumDetector().detect_single_seria(values))
                    self.assertLess(np.mean(scores[100:] > 0.5), 0.01)

    def test_constant_series_is_exactly_zero(self):
        for value in (0.0, 100.0, -5.0):
            detector = CusumDetector()
            self.assertEqual(detector.detect_single_seria([value] * 200), [0.0] * 200)
            for _ in range(100):
                self.assertEqual(detector.update(value), 0.0)
            self.assertTrue(detector.ready)

    def test_ramp_and_permanent_level_steps_are_not_bursts(self):
        for values in (list(range(300)), [0.0] * 100 + [10.0] * 200,
                       [10.0] * 100 + [0.0] * 200):
            self.assertEqual(CusumDetector().detect_single_seria(values), [0.0] * 300)

    def test_score_expires_when_excursion_leaves_current_window(self):
        detector = CusumDetector(window=10).fit([100.0] * 50)
        values = [100.0] * 15 + [50.0] + [100.0] * 20
        scores = detector.detect_single_seria(values)
        self.assertEqual(scores[15], 0.0)  # Still only one direction.
        self.assertGreater(scores[16], 0.5)
        self.assertEqual(scores[25:], [0.0] * len(scores[25:]))

    def test_window_controls_which_reversals_are_retained(self):
        data = [100.0] * 15 + [50.0] * 12 + [100.0] * 20
        short = CusumDetector(window=5).fit([100.0] * 50).detect_single_seria(data)
        long = CusumDetector(window=20).fit([100.0] * 50).detect_single_seria(data)
        self.assertEqual(max(short), 0.0)
        self.assertGreater(long[27], 0.5)

    def test_explicit_background_controls_sensitivity(self):
        rng = np.random.default_rng(43)
        background = rng.normal(100, 0.4, 101)
        window = [100.0] * 5 + [75.0, 50.0, 75.0, 100.0, 100.0]
        quiet = CusumDetector().fit(background).score(window)
        noisy = CusumDetector().fit(100 + (background - 100) * 30).score(window)
        self.assertGreater(quiet, 0.5)
        self.assertLess(noisy, 0.1)

    def test_background_is_not_contaminated_by_repeating_saw(self):
        detector = CusumDetector().fit([100.0] * 101)
        for value in [100.0] * 10 + [100.0, 95.0, 90.0, 95.0, 100.0] * 100:
            detector.update(value)
        self.assertEqual(detector.noise_std, detector.noise_floor)
        self.assertGreater(detector.update(100.0), 0.99)

    def test_batch_and_stream_replay_match(self):
        data = saw_scenario()
        stream = CusumDetector()
        streamed = [stream.update(value) for value in data]
        batch = CusumDetector().detect_single_seria(iter(data))
        self.assertEqual(streamed, batch)
        self.assertEqual(CusumDetector().detect(data), batch[-1])
        self.assertEqual(CusumDetector().score(data), batch[-1])

    def test_future_values_do_not_change_past_scores(self):
        values = saw_scenario()[:500]
        detector = CusumDetector()
        prefix = detector.detect_single_seria(values)
        extended = detector.detect_single_seria([*values, 1e9, -1e9, 0.0])
        self.assertEqual(prefix, extended[:len(prefix)])

    def test_scale_and_sign_invariance(self):
        values = saw_scenario()
        detector = CusumDetector()
        expected = detector.detect_single_seria(values)
        for altered in (values * 100, -values):
            np.testing.assert_allclose(detector.detect_single_seria(altered), expected,
                                       rtol=1e-9, atol=1e-9)

    def test_same_absolute_excursions_at_higher_level_are_less_severe(self):
        values = saw_scenario()
        original = np.asarray(CusumDetector().detect_single_seria(values))
        higher = np.asarray(CusumDetector().detect_single_seria(values + 1000))
        self.assertLess(higher[350:650].max(), 0.2)
        self.assertGreater(original[500:650].mean(), 0.8)

    def test_similar_brief_shallow_dips_stay_below_point_two(self):
        shape = [89, 97, 95, 94, 96, 99, 89, 97, 95, 94]
        for seed in (11, 22, 33, 43, 55):
            for noise_std in (0.0, 0.1, 0.4, 1.0):
                with self.subTest(seed=seed, noise_std=noise_std):
                    values = np.array([100.0] * 100 + shape + [100.0] * 100)
                    values += np.random.default_rng(seed).normal(0, noise_std, len(values))
                    scores = np.asarray(CusumDetector().detect_single_seria(values))
                    self.assertLessEqual(scores[100:120].max(), 0.2)

    def test_small_repeated_dips_accumulate_but_isolated_dip_does_not(self):
        detector = CusumDetector().fit([100.0] * 101)
        isolated = [100.0] * 20 + [95.0] + [100.0] * 20
        self.assertLessEqual(max(detector.detect_single_seria(isolated)), 0.2)
        repeated = [100.0] * 20 + [100.0, 95.0, 100.0, 100.0, 100.0] * 100
        scores = detector.detect_single_seria(repeated)
        self.assertLessEqual(max(scores[20:30]), 0.2)
        self.assertGreater(min(scores[-100:]), 0.9)

    def test_quiet_window_clears_inertia_before_another_small_dip(self):
        detector = CusumDetector().fit([100.0] * 101)
        repeated = [100.0, 95.0, 100.0, 100.0, 100.0] * 100
        for value in [100.0] * 20 + repeated:
            detector.update(value)
        self.assertGreater(detector.update(100.0), 0.9)
        quiet = [detector.update(100.0) for _ in range(detector.window)]
        self.assertEqual(quiet[-1], 0.0)
        scores = [detector.update(value) for value in [95.0] + [100.0] * 10]
        self.assertLessEqual(max(scores), 0.2)

    def test_inertia_history_is_bounded_and_controls_repetition_gain(self):
        values = [100.0] * 20 + [100.0, 95.0, 100.0, 100.0, 100.0] * 100
        short = CusumDetector(inertia_window=10).fit([100.0] * 101)
        long = CusumDetector(inertia_window=200).fit([100.0] * 101)
        self.assertLessEqual(max(short.detect_single_seria(values)), 0.2)
        self.assertGreater(long.detect(values), 0.9)
        for value in values:
            long.update(value)
        self.assertLessEqual(len(long._inertia), 200)

    def test_background_level_follows_quiet_plateau_before_saw(self):
        detector = CusumDetector()
        for value in saw_scenario()[:345]:
            detector.update(value)
        self.assertAlmostEqual(detector.background_level, 200, delta=1.0)

    def test_warmup_reset_and_independent_batch_calls(self):
        detector = CusumDetector()
        self.assertFalse(detector.ready)
        self.assertIsNone(detector.noise_std)
        for value in range(29):
            self.assertEqual(detector.update(value), 0.0)
        self.assertFalse(detector.ready)
        detector.update(29)
        self.assertTrue(detector.ready)
        before = detector.noise_std
        detector.detect_single_seria(saw_scenario())
        self.assertTrue(detector.ready)
        self.assertEqual(before, detector.noise_std)
        detector.reset()
        self.assertFalse(detector.ready)
        self.assertIsNone(detector.noise_std)
        detector.fit([0.0] * 21).reset()
        self.assertEqual(detector.noise_std, detector.noise_floor)
        self.assertEqual(detector.detect([]), 0.0)
        self.assertEqual(detector.detect_single_seria([]), [])

    def test_invalid_parameters_and_values_are_rejected(self):
        for kwargs in ({'threshold': 0}, {'threshold': math.inf}, {'drift': -1},
                       {'drift': math.nan}, {'window': 2}, {'window': True},
                       {'window': 3.5}, {'noise_floor': 0}, {'update_sigma': 0},
                       {'reference_window': 10}, {'min_reference_samples': 1},
                       {'relative_threshold': 0}, {'relative_threshold': math.inf},
                       {'relative_drift': -1}, {'relative_drift': math.nan},
                       {'inertia_window': 9}, {'inertia_window': True},
                       {'inertia_window': 10.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CusumDetector(**kwargs)
        detector = CusumDetector()
        detector.update(100.0)
        for value in (math.nan, math.inf, -math.inf, True, '100', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                detector.update(value)
        self.assertEqual(detector.update(100.0), 0.0)
        self.assertEqual(len(detector._reference), 1)
        for reference in ([1.0] * 20, [math.nan] * 21):
            with self.assertRaises(ValueError):
                detector.fit(reference)

    def test_extreme_finite_values_keep_score_bounded(self):
        for threshold in (1e-300, 1e300):
            detector = CusumDetector(threshold=threshold).fit([0.0] * 21)
            scores = detector.detect_single_seria([0.0] * 10 + [1e100, 0.0] + [0.0] * 10)
            self.assertTrue(all(math.isfinite(s) and 0 <= s <= 1 for s in scores))
        detector = CusumDetector()
        detector.update(-1e308)
        with self.assertRaises(ValueError):
            detector.update(1e308)
        self.assertEqual(detector._previous, -1e308)
        detector = CusumDetector().fit([1e308] * 21)
        self.assertEqual(detector.background_level, 1e308)
        huge = detector.detect_single_seria([1e308] * 10 + [5e307, 1e308] + [1e308] * 10)
        ordinary = CusumDetector().fit([100.0] * 21).detect_single_seria(
            [100.0] * 10 + [50.0, 100.0] + [100.0] * 10)
        self.assertAlmostEqual(max(huge), max(ordinary), places=6)


if __name__ == '__main__':
    unittest.main()
