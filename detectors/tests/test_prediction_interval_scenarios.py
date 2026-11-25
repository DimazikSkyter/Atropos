import math
import unittest

import numpy as np

from apps.detectors import PredictionIntervalDetector
from tests.acf_scenarios import cycle_change_scenario, persistence_scenarios
from tests.cusum_scenarios import legacy_scenarios, noisy_scenario, saw_scenario
from tests.prediction_interval_scenarios import previous_report_scenarios, reconstructed_rps_15s
from tests.scenarios import response
from tests.visualize_correlation import anomaly_scenarios
from tests.visualize_generator import scenarios as generator_scenarios


class PredictionIntervalScenarioTests(unittest.TestCase):
    def test_all_previous_report_series_against_independent_numpy_bounds(self):
        scenarios = list(previous_report_scenarios())
        self.assertGreaterEqual(len(scenarios), 50)
        self.assertTrue({'acf', 'cusum', 'runtime-trend', 'correlation', 'generator'}
                        <= {s.source for s in scenarios})
        for scenario in scenarios:
            for auto_scale in (False, True):
                with self.subTest(scenario=scenario.name, auto_scale=auto_scale):
                    n = scenario.window
                    detector = PredictionIntervalDetector(n, auto_scale=auto_scale)
                    results = detector.predict_intervals(scenario.values)
                    self.assertEqual(len(results), len(scenario.values))
                    self.assertTrue(all(not r.ready for r in results[:n]))
                    scored = results[n:]
                    windows = np.lib.stride_tricks.sliding_window_view(scenario.values, n+1)[:, :n]
                    centers = windows.mean(axis=1)
                    # Tabulated t_(.975, n-1), sample variance and next-point factor.
                    critical = {8: 2.364624251592784, 30: 2.045229642132703}[n]
                    widths = critical * windows.std(axis=1, ddof=1) * math.sqrt(1+1/n)
                    widths = np.maximum(widths, 1e-9) * [r.scale for r in scored]
                    np.testing.assert_allclose([r.infimum for r in scored], centers-widths,
                                               rtol=1e-10, atol=1e-10)
                    np.testing.assert_allclose([r.supremum for r in scored], centers+widths,
                                               rtol=1e-10, atol=1e-10)
                    for value, result in zip(scenario.values[n:], scored):
                        self.assertTrue(result.ready)
                        self.assertTrue(0 <= result.score < 1)
                        self.assertEqual(result.score == 0,
                                         result.infimum <= value <= result.supremum)

    def test_15_second_screenshot_reconstruction_matches_previous_export_landmarks(self):
        values = reconstructed_rps_15s()
        self.assertEqual(len(values), 5171)
        np.testing.assert_allclose(values[:4], [987.38, 984.334869, 981.289738, 978.244607],
                                   atol=1e-6, rtol=0)
        self.assertAlmostEqual(values.min(), 482.738532, places=6)
        self.assertAlmostEqual(values.max(), 1009.966159, places=6)

    def test_runtime_response_spike_uses_two_minute_history_before_first_event(self):
        values = [v for _, v in response('spike')]
        results = PredictionIntervalDetector(8).predict_intervals(values)
        self.assertGreater(results[24].score, 0.99)
        self.assertEqual(results[34].score, 0)

    def test_legacy_dips_are_flagged_at_their_first_deviation(self):
        # Unlike a relative CUSUM burst score, even a shallow but unexpected
        # dip is an outlier relative to a quiet preceding window.
        for name, values, start, _ in legacy_scenarios():
            with self.subTest(name=name):
                result = PredictionIntervalDetector().predict_intervals(values)[start]
                self.assertGreater(result.score, 0.5)
                self.assertLess(values[start], result.infimum)

    def test_same_saw_excursion_is_less_surprising_on_noisy_background(self):
        quiet = PredictionIntervalDetector().predict_intervals(saw_scenario())
        noisy = PredictionIntervalDetector().predict_intervals(noisy_scenario())
        self.assertGreater(max(r.score for r in quiet[350:354]), 0.5)
        self.assertGreater(max(r.score for r in quiet[350:354]),
                           max(r.score for r in noisy[350:354]))

    def test_long_plateau_is_eventually_the_new_predicted_window(self):
        for window in (30, 40):
            with self.subTest(window=window):
                values = [100.0]*100 + [200.0]*100 + [100.0]*100
                results = PredictionIntervalDetector(window).predict_intervals(values)
                self.assertGreater(results[100].score, 0.99)
                self.assertGreater(results[200].score, 0.99)
                self.assertEqual(results[100+window].score, 0)
                self.assertAlmostEqual(results[100+window].infimum, 200, places=7)
                self.assertEqual(results[200+window].score, 0)
                self.assertAlmostEqual(results[200+window].supremum, 100, places=7)

    def test_previous_acf_plateaus_and_recoveries_return_to_baseline(self):
        for name, _, _, values, start, end in persistence_scenarios():
            if not name.startswith(('plateau_', 'recovery_')):
                continue
            with self.subTest(name=name):
                results = PredictionIntervalDetector().predict_intervals(values)
                self.assertGreater(results[start].score, 0.7)
                self.assertLess(np.mean([r.score > 0 for r in results[end+60:]]), 0.1)

    def test_period_changes_preserve_causal_outputs_across_noise_seeds(self):
        for kind in ('slower', 'faster', 'irregular', 'gradual', 'amplitude'):
            for seed in (11, 43, 55):
                with self.subTest(kind=kind, seed=seed):
                    _, _, _, values, start, _ = cycle_change_scenario(kind, seed)
                    detector = PredictionIntervalDetector(auto_scale=True)
                    prefix = detector.predict_intervals(values[:start+30])
                    full = detector.predict_intervals(values)
                    self.assertEqual(prefix, full[:len(prefix)])

    def test_generator_injections_do_not_affect_bounds_until_after_first_changed_point(self):
        for label, original, modified, interval in generator_scenarios():
            if interval is None:
                continue
            with self.subTest(label=label):
                start, _ = interval
                detector = PredictionIntervalDetector()
                original_results = detector.predict_intervals(original.values)
                changed_results = detector.predict_intervals(modified.values)
                self.assertEqual(original_results[:start], changed_results[:start])
                self.assertEqual(original_results[start].infimum, changed_results[start].infimum)
                self.assertEqual(original_results[start].supremum, changed_results[start].supremum)
                self.assertGreater(changed_results[start].score, 0.8)

    def test_correlation_report_twenty_percent_burst_is_seen_and_recovers(self):
        for name, _, _, metric, metadata in anomaly_scenarios():
            if name != 'burst-20-percent':
                continue
            start, end = metadata['anomaly_range']
            results = PredictionIntervalDetector().predict_intervals(metric.values)
            self.assertGreater(results[start].score, 0.8)
            self.assertLess(np.mean([r.score > 0 for r in results[end+30:]]), 0.15)

    def test_variance_step_expands_forecasts_then_recovers_coverage(self):
        rng = np.random.default_rng(43)
        values = np.r_[rng.normal(0, 0.1, 500), rng.normal(0, 3, 500)]
        results = PredictionIntervalDetector().predict_intervals(values)
        widths = np.array([r.supremum-r.infimum if r.ready else np.nan for r in results])
        self.assertGreater(results[500].score, 0.5)
        self.assertGreater(np.mean(widths[700:950]), 20*np.mean(widths[200:450]))
        self.assertLess(np.mean([r.score > 0 for r in results[700:950]]), 0.1)


if __name__ == '__main__':
    unittest.main()
