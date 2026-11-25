import math
import unittest

import numpy as np

from apps.detectors import MadDetector


def dispersion_change(seed=43):
    rng = np.random.default_rng(seed)
    return np.r_[rng.normal(0, 0.1, 500), rng.normal(0, 3.0, 500)]


class MadTests(unittest.TestCase):
    def test_matches_original_formula_on_rolling_fragments(self):
        values = dispersion_change()
        for local_size, reference_size in ((20, 100), (20, 99), (5, 40)):
            with self.subTest(local=local_size, reference=reference_size):
                scores = MadDetector(local_size, reference_size).detect_single_seria(values)
                self.assertEqual(len(scores), len(values))
                self.assertEqual(scores[:reference_size - 1], [0.0] * (reference_size - 1))
                for i in range(reference_size - 1, len(values)):
                    reference = values[i - reference_size + 1:i + 1]
                    local = values[i - local_size + 1:i + 1]
                    global_mad = np.mean(np.abs(reference - np.mean(reference)))
                    local_mad = np.mean(np.abs(local - np.mean(local)))
                    relative = abs(local_mad - global_mad) / global_mad
                    expected = 2 / (1 + np.exp(-relative)) - 1
                    self.assertAlmostEqual(scores[i], expected, places=12)

    def test_dispersion_jump_reacts_then_adapts(self):
        for seed in (11, 43, 55):
            with self.subTest(seed=seed):
                scores = np.asarray(MadDetector().detect_single_seria(dispersion_change(seed)))
                self.assertLess(scores[200:450].mean(), 0.15)
                self.assertGreater(scores[500:540].max(), 0.5)
                self.assertLess(scores[700:950].mean(), 0.15)
                self.assertGreater(scores[500:540].max(), 3 * scores[700:950].mean())

    def test_original_increasing_dispersion_scenario(self):
        rng = np.random.RandomState(43)
        values = np.r_[rng.normal(0, 0.1, 1000),
                       rng.normal(0, (np.arange(1000) + 10) / 100)]
        scores = np.asarray(MadDetector().detect_single_seria(values))
        self.assertGreater(scores[1000:1120].max(), 0.4)
        self.assertLess(scores[1500:].mean(), 0.2)

    def test_reacts_to_a_decrease_in_dispersion_as_well(self):
        rng = np.random.default_rng(43)
        values = np.r_[rng.normal(0, 3, 500), rng.normal(0, 0.1, 500)]
        scores = np.asarray(MadDetector().detect_single_seria(values))
        self.assertGreater(scores[520:560].max(), 0.4)
        self.assertLess(scores[750:950].mean(), 0.15)

    def test_batch_stream_and_final_score_agree(self):
        values = dispersion_change()
        stream = MadDetector(window=15, data_max_size=80)
        expected = [stream.update(value) for value in values]
        batch = MadDetector(window=15, data_max_size=80)
        self.assertEqual(batch.detect_single_seria(iter(values)), expected)
        self.assertEqual(batch.detect(values), expected[-1])
        self.assertEqual(batch.score(values), expected[-1])

    def test_past_scores_do_not_use_future_samples(self):
        values = dispersion_change()
        detector = MadDetector()
        prefix = detector.detect_single_seria(values[:550])
        extended = detector.detect_single_seria([*values[:550], 1e9, -1e9])
        self.assertEqual(prefix, extended[:len(prefix)])

    def test_exact_reference_size_and_final_sample_are_not_lost(self):
        detector = MadDetector(window=20, data_max_size=100)
        values = [0.0] * 100 + [10.0]
        scores = [detector.update(v) for v in values]
        self.assertEqual(scores[:100], [0.0] * 100)
        self.assertGreater(scores[-1], 0.5)
        self.assertEqual(len(detector._reference), 100)
        self.assertAlmostEqual(detector.reference_mad, 0.198)
        self.assertAlmostEqual(detector.local_mad, 0.95)
        self.assertEqual(len(scores), len(values))

    def test_constant_reference_and_equal_window_sizes_are_zero(self):
        for value in (0.0, 100.0, -5.0, 1e308):
            with self.subTest(value=value):
                detector = MadDetector()
                self.assertEqual(detector.detect_single_seria([value] * 200), [0.0] * 200)
        values = dispersion_change()
        self.assertEqual(MadDetector(20, 20).detect_single_seria(values), [0.0] * len(values))

    def test_warmup_reset_and_batch_calls_do_not_mutate_stream(self):
        detector = MadDetector()
        self.assertFalse(detector.ready)
        self.assertIsNone(detector.local_mad)
        self.assertIsNone(detector.reference_mad)
        for value in range(99):
            self.assertEqual(detector.update(value), 0.0)
        self.assertFalse(detector.ready)
        detector.update(99)
        self.assertTrue(detector.ready)
        before = (list(detector._reference), detector.local_mad, detector.reference_mad)
        detector.detect_single_seria(dispersion_change())
        self.assertEqual(before, (list(detector._reference), detector.local_mad, detector.reference_mad))
        self.assertEqual(detector.detect_single_seria([]), [])
        self.assertEqual(detector.detect([]), 0.0)
        detector.reset()
        self.assertFalse(detector.ready)
        self.assertIsNone(detector.reference_mad)
        self.assertEqual(detector.update(1.0), 0.0)

    def test_invalid_parameters_and_values_are_rejected_without_mutation(self):
        for kwargs in ({'window': 1}, {'window': True}, {'window': 2.5},
                       {'data_max_size': 1}, {'data_max_size': False},
                       {'data_max_size': 99.5}, {'window': 21, 'data_max_size': 20}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MadDetector(**kwargs)
        detector = MadDetector()
        for value in range(100):
            detector.update(value)
        before = (list(detector._reference), detector.local_mad, detector.reference_mad)
        for value in (math.nan, math.inf, -math.inf, True, '100', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                detector.update(value)
            self.assertEqual(before, (list(detector._reference), detector.local_mad, detector.reference_mad))

    def test_translation_scale_and_sign_invariance(self):
        values = dispersion_change()
        detector = MadDetector()
        expected = detector.detect_single_seria(values)
        for changed in (values + 473.6, values * 100, -values, values * 1e-100):
            np.testing.assert_allclose(detector.detect_single_seria(changed), expected,
                                       atol=1e-10, rtol=1e-10)

    def test_extreme_finite_inputs_keep_deviations_and_scores_finite(self):
        for values in ([1e308, -1e308] * 100,
                       [1e308] * 100 + [-1e308] * 100,
                       [0.0] * 100 + [1e-320] * 100):
            with self.subTest(values=values[:2]):
                detector = MadDetector()
                scores = [detector.update(value) for value in values]
                self.assertTrue(all(math.isfinite(v) and 0 <= v <= 1 for v in scores))
                self.assertTrue(math.isfinite(detector.local_mad))
                self.assertTrue(math.isfinite(detector.reference_mad))


if __name__ == '__main__':
    unittest.main()
