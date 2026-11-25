import math
import unittest

from apps.runtime_trend import RuntimeTrendDetector, TrendConfig
from tests.scenarios import cpu_load, dirty_latency, gc_saw, response


def replay(points, config, every=60):
    detector = RuntimeTrendDetector(config)
    results = []
    for timestamp, value in points:
        detector.update(timestamp, value)
        if timestamp % every == 0:
            results.append(detector.evaluate())
    return results


class RuntimeTrendTests(unittest.TestCase):
    def test_gc_multiple_saws_without_drift(self):
        results = replay(gc_saw(), TrendConfig(1800, 100, 0.8))
        self.assertTrue(any(r.ready for r in results))
        self.assertFalse(any(r.detected for r in results))

    def test_gc_multiple_saws_with_drift(self):
        results = replay(gc_saw(drift=1.5), TrendConfig(1800, 100, 0.8))
        ready = [r for r in results if r.ready]
        self.assertGreater(sum(r.detected for r in ready) / len(ready), 0.9)
        self.assertTrue(all(r.direction == 'up' for r in ready if r.detected))

    def test_volatile_cpu_sixty_percentage_points_over_ten_minutes(self):
        cfg = TrendConfig(600, 160, 4.0)
        results = replay(cpu_load(), cfg)
        self.assertTrue(any(r.detected and r.direction == 'up'
                            and 12 * 60 <= r.timestamp <= 16 * 60 for r in results))
        self.assertFalse(any(r.detected for r in results if r.timestamp >= 26 * 60))
        strict = replay(cpu_load(), TrendConfig(600, 0.01, 4.0))
        self.assertFalse(any(r.detected for r in strict))

    def test_quiet_response_detected_within_two_minutes_of_growth(self):
        results = replay(response('quiet_rise'), TrendConfig(120, 0.1, 1.0))
        alarm = next(r for r in results if r.detected)
        self.assertGreaterEqual(alarm.timestamp, 4 * 60)
        self.assertLessEqual(alarm.timestamp, 6 * 60)

    def test_spikes_and_level_steps_are_not_trends(self):
        for kind in ('flat', 'spike', 'step', 'temporary_step'):
            with self.subTest(kind=kind):
                results = replay(response(kind), TrendConfig(120, 1, 1.0))
                self.assertFalse(any(r.detected for r in results))
                if kind in ('step', 'temporary_step'):
                    self.assertTrue(any(r.step_like for r in results))

    def test_backpressure_detects_growth_and_recovery(self):
        results = replay(response('backpressure'), TrendConfig(120, 1, 3.0))
        self.assertTrue(any(r.detected and r.direction == 'up'
                            and 5 * 60 <= r.timestamp <= 8 * 60 for r in results))
        self.assertTrue(any(r.detected and r.direction == 'down'
                            and 9 * 60 <= r.timestamp <= 12 * 60 for r in results))
        self.assertFalse(any(r.detected for r in results if r.timestamp >= 15 * 60))

    def test_dirty_stationary_latency_parameter_tradeoffs(self):
        points = dirty_latency()
        sensitive = replay(points, TrendConfig(120, 10, 0.005))
        amplitude_guard = replay(points, TrendConfig(120, 10, 2.0))
        variance_guard = replay(points, TrendConfig(120, 0.0001, 0.005))
        long_window = replay(points, TrendConfig(1800, 10, 0.03))
        # Sensitive alarms are deliberately FALSE POSITIVES on this stationary fixture.
        self.assertTrue(any(r.detected for r in sensitive))
        for results in (amplitude_guard, variance_guard, long_window):
            self.assertTrue(any(r.ready for r in results))
            self.assertFalse(any(r.detected for r in results))

    def test_dirty_latency_multiple_seeds_conservative_config(self):
        for seed in (11, 22, 33, 44, 55):
            with self.subTest(seed=seed):
                results = replay(dirty_latency(seed=seed), TrendConfig(1800, 10, 0.03))
                self.assertFalse(any(r.detected for r in results))

    def test_units_and_irregular_timestamps(self):
        points = [(t, 3 + 2 * t / 60) for t in (0, 11, 29, 45, 63, 79, 95, 108, 120)]
        detector = RuntimeTrendDetector(TrendConfig(120, 1, 0.5, value_scale=2))
        for t, v in points:
            detector.update(t, v)
        result = detector.evaluate()
        self.assertTrue(result.detected)
        self.assertAlmostEqual(result.tangent, 1, places=6)

    def test_confirmation_and_repeated_evaluation(self):
        detector = RuntimeTrendDetector(TrendConfig(120, 1, 0.5, confirmation=2))
        for i in range(9):
            detector.update(i * 15, i)
        first = detector.evaluate()
        self.assertEqual(first.reason, 'confirmation')
        self.assertIs(detector.evaluate(), first)
        detector.update(135, 9)
        self.assertTrue(detector.evaluate().detected)
        detector.update(1000, 100)
        self.assertEqual(detector.evaluate().reason, 'warmup')

    def test_invalid_inputs(self):
        for args in ((0, 1, 1), (120, -1, 1), (120, 1, math.nan)):
            with self.assertRaises(ValueError):
                TrendConfig(*args)
        detector = RuntimeTrendDetector(TrendConfig(120, 1, 1))
        with self.assertRaises(ValueError):
            detector.evaluate()
        detector.update(0, 1)
        for t, value in ((0, 2), (-1, 2), (15, math.nan), (15, math.inf)):
            with self.assertRaises(ValueError):
                detector.update(t, value)

    def test_bounded_memory_and_warmup(self):
        detector = RuntimeTrendDetector(TrendConfig(120, 1, 1, max_samples=8))
        for i in range(1000):
            detector.update(i, i)
        result = detector.evaluate()
        self.assertEqual(result.samples, 8)
        self.assertFalse(result.ready)


if __name__ == '__main__':
    unittest.main()
