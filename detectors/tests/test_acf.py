import math
import unittest
from dataclasses import FrozenInstanceError

import numpy as np
from statsmodels.tsa.stattools import acf

from apps.detectors import ACFDetector
from tests.acf_scenarios import ar_series


class ACFTests(unittest.TestCase):
    def test_window_centered_profile_matches_statsmodels(self):
        rng = np.random.default_rng(43)
        reference, values = rng.normal(size=512), rng.normal(size=300)
        detector = ACFDetector().fit(reference)
        for i, value in enumerate(values):
            detector.update(value)
            if detector.ready:
                expected = acf(values[i - 255:i + 1], nlags=32, fft=False, adjusted=False)
                np.testing.assert_allclose(detector.result.acf, expected, atol=1e-14)
        np.testing.assert_allclose(detector.result.reference_acf,
                                   acf(reference, nlags=32, fft=False), atol=1e-14)

    def test_window_centered_profile_matches_direct_products_on_level_change(self):
        rng = np.random.default_rng(43)
        reference = rng.normal(10, 1, 512)
        values = rng.normal(14, 1, 256)
        result = ACFDetector().fit(reference).analyze(values)
        residuals = values - values.mean()
        expected = [np.dot(residuals[:256-k], residuals[k:]) /
                    np.dot(residuals, residuals) for k in range(33)]
        np.testing.assert_allclose(result.acf, expected, atol=1e-14)

    def test_impulse_plateau_depth_and_mass_have_analytic_oracle(self):
        for duration in (1, 4, 16, 64, 256):
            with self.subTest(duration=duration):
                values = [0.0] * (256-duration) + [1.0] * duration
                result = ACFDetector().fit([0.0] * 512).analyze(values)
                band = 3.5 * math.sqrt(1/256 + 1/512)
                # Independent closed form: zero/zero, one/one and cross-level
                # pairs in a binary window, after subtracting p=duration/N.
                p = duration/256
                energy = 256*p*(1-p)
                expected = []
                for k in range(33):
                    zeros, ones = max(0, 256-duration-k), max(0, duration-k)
                    cross = 256-k-zeros-ones
                    numerator = zeros*p*p + ones*(1-p)**2 - cross*p*(1-p)
                    expected.append(numerator/energy if energy else 0)
                significant = tuple(k for k in range(1, 33) if expected[k] > band)
                mass = sum(max(0, expected[k]-band) for k in range(1, 33))
                np.testing.assert_allclose(result.acf, expected, atol=1e-14)
                self.assertEqual(result.significant_lags, significant)
                self.assertEqual(result.depth, max(significant, default=0))
                self.assertAlmostEqual(result.lag_mass, mass)
                self.assertAlmostEqual(result.score, mass/32)
                self.assertEqual(result.horizon_reached, result.depth == 32)

    def test_longer_effect_has_greater_depth_and_score(self):
        results = [ACFDetector().fit([0.0]*512).analyze(
            [0.0]*(256-d) + [1.0]*d) for d in (1, 4, 16, 64)]
        self.assertEqual(results[0].score, 0)
        self.assertEqual(results[0].depth, 0)
        self.assertTrue(all(a.depth < b.depth for a, b in zip(results, results[1:])))
        self.assertTrue(all(a.score < b.score for a, b in zip(results, results[1:])))

    def test_recovery_time_orders_persistence(self):
        results = []
        for tau in (0.5, 2, 8, 24):
            values = np.r_[np.zeros(128), np.exp(-np.arange(128)/tau)]
            results.append(ACFDetector().fit(np.zeros(512)).analyze(values))
        self.assertTrue(all(a.depth < b.depth for a, b in zip(results, results[1:])))
        self.assertTrue(all(a.score < b.score for a, b in zip(results, results[1:])))

    def test_constant_changed_level_becomes_new_norm_after_one_window(self):
        detector = ACFDetector().fit([0.0]*512)
        for value in [0.0]*128 + [2.0]*128:
            detector.update(value)
        self.assertGreater(detector.result.score, 0.4)
        self.assertEqual(detector.result.depth, 32)
        self.assertFalse(detector.result.zero_energy)
        for _ in range(128):
            detector.update(2.0)
        self.assertEqual(detector.result.score, 0)
        self.assertTrue(detector.result.zero_energy)
        self.assertTrue(detector.result.reference_zero_energy)

    def test_40_samples_at_15_seconds_forget_a_step_in_10_minutes(self):
        detector = ACFDetector(window=40, max_lag=10, reference_window=160).fit([0]*160)
        for value in [0]*40:
            detector.update(value)
        scores = [detector.update(7.0) for _ in range(40)]
        self.assertGreater(max(scores), 0.05)
        self.assertEqual(scores[-1], 0)
        self.assertTrue(detector.result.zero_energy)
        self.assertEqual(detector.result.depth, 0)

    def test_amplitude_and_sign_of_isolated_event_do_not_set_its_depth(self):
        expected = ACFDetector().fit([0.0]*512).analyze([0.0]*240 + [1.0]*16)
        for amplitude in (0.001, 1000, -1e100, 1e-200):
            result = ACFDetector().fit([0.0]*512).analyze(
                [0.0]*240 + [amplitude]*16)
            np.testing.assert_allclose(result.acf, expected.acf, atol=1e-14)
            self.assertAlmostEqual(result.score, expected.score)
            self.assertEqual(result.depth, expected.depth)

    def test_white_noise_has_low_empirical_alarm_rate(self):
        rates = []
        for seed in range(10):
            rng = np.random.default_rng(seed)
            detector = ACFDetector().fit(rng.normal(size=512))
            scores = np.asarray(detector.detect_single_seria(rng.normal(size=1000)))[255:]
            rates.append(np.mean(scores > 0))
        self.assertLess(np.mean(rates), 0.02)
        self.assertLess(max(rates), 0.1)

    def test_unchanged_autocorrelation_and_cycles_are_baseline(self):
        for seed in (11, 43, 55):
            detector = ACFDetector().fit(ar_series(0.8, 512, seed))
            scores = np.asarray(detector.detect_single_seria(ar_series(0.8, 1000, seed+1)))
            self.assertLess(np.mean(scores[255:] > 0.01), 0.05)
        healthy = np.sin(2*np.pi*np.arange(512)/16)
        values = np.sin(2*np.pi*np.arange(1000)/16)
        self.assertLess(max(ACFDetector().fit(healthy).detect_single_seria(values)), 1e-12)

    def test_new_and_disappearing_serial_dependence_are_detected(self):
        rng = np.random.default_rng(43)
        new = ACFDetector().fit(rng.normal(size=512)).analyze(ar_series(0.9, 256))
        lost = ACFDetector().fit(ar_series(0.9, 512)).analyze(rng.normal(size=256))
        self.assertGreater(new.score, 0.01)
        self.assertGreater(new.depth, 2)
        self.assertGreater(lost.score, 0.01)
        self.assertIn(1, lost.significant_lags)

    def test_alternating_dependence_and_nonadjacent_lag_are_visible(self):
        alternating = ACFDetector().fit([0.0]*512).analyze([1.0, -1.0]*128)
        self.assertGreater(alternating.score, 0.6)
        self.assertLess(alternating.acf[1], 0)
        # MA with its only theoretical nonzero ACF at lag 8.
        rng = np.random.default_rng(5)
        noise = rng.normal(size=264)
        values = noise[8:] + noise[:-8]
        delayed = ACFDetector().fit(rng.normal(size=512)).analyze(values)
        self.assertIn(8, delayed.significant_lags)
        self.assertNotIn(1, delayed.significant_lags)

    def test_variance_rescaling_alone_does_not_create_memory(self):
        rng = np.random.default_rng(43)
        values = rng.normal(size=512)
        # Repeat exactly the same finite pattern to isolate scale from sampling noise.
        detector = ACFDetector(window=256, reference_window=256).fit(values[:256])
        self.assertEqual(detector.score(values[:256]*100), 0)

    def test_affine_invariance_for_reference_and_stream(self):
        rng = np.random.default_rng(43)
        healthy = rng.normal(size=512)
        values = np.r_[rng.normal(size=280), np.full(40, 4.0)]
        expected = ACFDetector().fit(healthy).analyze(values)
        for scale, offset in ((100, 473.6), (-2, 25), (1e-100, 0), (1e100, 0)):
            result = ACFDetector().fit(healthy*scale+offset).analyze(values*scale+offset)
            np.testing.assert_allclose(result.acf, expected.acf, atol=1e-12)
            self.assertAlmostEqual(result.score, expected.score, places=12)

    def test_exact_automatic_and_fitted_warmup(self):
        detector = ACFDetector(window=16, max_lag=4, reference_window=32)
        for _ in range(31):
            self.assertEqual(detector.update(0), 0)
        self.assertFalse(detector.reference_ready)
        detector.update(0)
        self.assertTrue(detector.reference_ready)
        self.assertFalse(detector.ready)
        for _ in range(15):
            detector.update(1)
        self.assertFalse(detector.ready)
        detector.update(1)
        self.assertTrue(detector.ready)
        fitted = ACFDetector(window=16, max_lag=4, reference_window=32,
                             threshold=1.5).fit([0]*16)
        self.assertTrue(fitted.reference_ready)
        self.assertFalse(fitted.ready)
        self.assertGreater(fitted.detect([0]*8+[1]*8), 0)
        self.assertEqual(fitted.detect([1]*15), 0)

    def test_batch_stream_final_diagnostics_and_empty_input_agree(self):
        values = [0.0]*300 + [1.0]*100
        stream = ACFDetector().fit([0.0]*512)
        expected = [stream.update(v) for v in values]
        batch = ACFDetector().fit([0.0]*512)
        self.assertEqual(batch.detect_single_seria(iter(values)), expected)
        self.assertEqual(batch.analyze(iter(values)), stream.result)
        self.assertEqual(batch.detect(values), expected[-1])
        self.assertEqual(batch.score(values), expected[-1])
        self.assertEqual(batch.detect_single_seria([]), [])
        self.assertEqual(batch.detect([]), 0)
        self.assertFalse(batch.analyze([]).ready)

    def test_replays_are_causal_and_do_not_mutate_active_stream(self):
        detector = ACFDetector(window=16, max_lag=4, reference_window=32).fit([0]*32)
        prefix = [0]*20 + [1]*5
        scores = detector.detect_single_seria(prefix)
        self.assertEqual(scores, detector.detect_single_seria(prefix + [1e9, -1e9])[:25])
        for value in prefix:
            detector.update(value)
        before = (detector.result, tuple(detector._values), tuple(detector._reference))
        detector.analyze(range(100))
        detector.detect_single_seria(range(100))
        self.assertEqual(before, (detector.result, tuple(detector._values),
                                  tuple(detector._reference)))
        with self.assertRaises(FrozenInstanceError):
            detector.result.depth = 999

    def test_reference_is_fixed_and_reset_retains_only_explicit_fit(self):
        for fitted in (False, True):
            detector = ACFDetector(window=16, max_lag=4, reference_window=32)
            if fitted:
                detector.fit([0]*32)
            else:
                for _ in range(32):
                    detector.update(0)
            before = tuple(detector._reference)
            for value in [2]*500:
                detector.update(value)
            self.assertEqual(tuple(detector._reference), before)
            self.assertEqual(len(detector._values), 16)
            self.assertEqual(len(detector._reference), 32)
            detector.reset()
            self.assertFalse(detector.ready)
            self.assertEqual(detector.reference_ready, fitted)

    def test_fit_replaces_reference_uses_tail_and_resets_stream(self):
        detector = ACFDetector(window=16, max_lag=4, reference_window=32).fit([0]*32)
        for _ in range(20):
            detector.update(1)
        detector.fit([99]*50 + [1]*32)
        self.assertEqual(tuple(detector._reference), (1.0,)*32)
        self.assertFalse(detector.ready)
        self.assertEqual(detector.detect([1]*16), 0)

    def test_invalid_configurations(self):
        cases = ({'window': 7}, {'window': True}, {'window': 256.5},
                 {'max_lag': 0}, {'max_lag': True}, {'max_lag': 1.5},
                 {'max_lag': 65}, {'reference_window': 128},
                 {'reference_window': False}, {'threshold': 0},
                 {'threshold': math.inf}, {'threshold': True},
                 {'min_change': -0.1}, {'min_change': 2},
                 {'min_change': math.nan})
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ACFDetector(**kwargs)

    def test_invalid_observations_and_fit_are_transactional(self):
        detector = ACFDetector(window=16, max_lag=4, reference_window=32).fit([0]*32)
        for value in range(30):
            detector.update(value)
        before = (detector.result, tuple(detector._values), tuple(detector._reference),
                  detector._seed)
        for value in (math.nan, math.inf, -math.inf, True, None, '1', [1]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                detector.update(value)
            with self.subTest(reference=value), self.assertRaises(ValueError):
                detector.fit([0]*32 + [value])
            self.assertEqual(before, (detector.result, tuple(detector._values),
                                      tuple(detector._reference), detector._seed))
        with self.assertRaises(ValueError):
            detector.fit([0]*15)
        self.assertEqual(before[-1], detector._seed)

    def test_extreme_finite_values_and_large_dc_remain_finite(self):
        cases = (([1e308]*512, [-1e308]*256),
                 ([1e308, -1e308]*256, [1e308, -1e308]*128),
                 ([0.0]*512, [0.0]*240 + [1e-320]*16),
                 ([0.0]*512, [0.0]*240 + [1e308]*16),
                 ([1e15, 1e15+1]*256, [1e15, 1e15+2]*128))
        for reference, values in cases:
            with self.subTest(first=reference[0], last=values[-1]):
                result = ACFDetector().fit(reference).analyze(values)
                self.assertTrue(math.isfinite(result.score))
                self.assertTrue(0 <= result.score <= 1)
                self.assertTrue(all(math.isfinite(v) and -1 <= v <= 1 for v in result.acf))
                self.assertTrue(all(math.isfinite(v) for v in result.thresholds))


if __name__ == '__main__':
    unittest.main()
