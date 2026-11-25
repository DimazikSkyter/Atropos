"""Cycle changes, including the user's regular-to-disturbed waveform example."""

import unittest

import numpy as np

from apps.detectors import ACFDetector
from tests.acf_scenarios import cycle_change_scenario


class ACFCycleTests(unittest.TestCase):
    @staticmethod
    def replay(kind, seed):
        _, _, healthy, values, start, end = cycle_change_scenario(kind, seed)
        detector = ACFDetector().fit(healthy)
        scores = np.asarray([detector.update(value) for value in values])
        return scores, start, end, detector.window

    def assert_healthy_before_and_after(self, scores, start, end, window):
        self.assertEqual(float(scores[window-1:start].max()), 0)
        self.assertEqual(float(scores[end+window:].max()), 0)

    def test_shorter_and_longer_periods_are_detected_until_recovery(self):
        for kind in ('slower', 'faster'):
            for seed in (11, 43, 55):
                with self.subTest(kind=kind, seed=seed):
                    scores, start, end, window = self.replay(kind, seed)
                    stable_change = scores[start+window-1:end]
                    self.assertGreater(float(stable_change.mean()), 0.1)
                    self.assertGreater(float(np.mean(stable_change > 0.05)), 0.95)
                    self.assert_healthy_before_and_after(scores, start, end, window)

    def test_irregular_cycle_lengths_are_detected(self):
        for seed in (11, 43, 55):
            with self.subTest(seed=seed):
                scores, start, end, window = self.replay('irregular', seed)
                stable_change = scores[start+window-1:end]
                self.assertGreater(float(stable_change.mean()), 0.03)
                self.assertGreater(float(np.mean(stable_change > 0.01)), 0.95)
                self.assert_healthy_before_and_after(scores, start, end, window)

    def test_gradual_period_change_with_growing_amplitude(self):
        for seed in (11, 43, 55):
            with self.subTest(seed=seed):
                scores, start, end, window = self.replay('gradual', seed)
                stable_change = scores[start+window-1:end]
                self.assertGreater(float(stable_change.mean()), 0.05)
                self.assertGreater(float(np.mean(stable_change > 0.01)), 0.9)
                self.assert_healthy_before_and_after(scores, start, end, window)

    def test_amplitude_growth_without_cycle_change_is_a_negative_control(self):
        for seed in (11, 43, 55):
            with self.subTest(seed=seed):
                scores, start, end, window = self.replay('amplitude', seed)
                self.assertEqual(float(scores.max()), 0)
                self.assert_healthy_before_and_after(scores, start, end, window)

    def test_period_doubling_reverses_the_known_lag_16_correlation(self):
        healthy = np.sin(2*np.pi*np.arange(512)/16)
        changed = np.sin(2*np.pi*np.arange(256)/32)
        result = ACFDetector().fit(healthy).analyze(changed)
        # At 16 samples the old waveform repeats, the new one is half a cycle
        # away. This independently verifies that the fixture changes memory.
        self.assertGreater(result.reference_acf[16], 0.9)
        self.assertLess(result.acf[16], -0.9)
        self.assertIn(16, result.significant_lags)
        self.assertGreater(result.score, 0.1)


if __name__ == '__main__':
    unittest.main()
