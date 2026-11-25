"""Offline regression on the RPS series selected in the user's Grafana image."""

from dataclasses import replace
import math
import unittest

import numpy as np

from tests.acf_replay_scenarios import SETTINGS
from tests.acf_rps_scenario import load_snapshot
from tests.visualize_acf_replays import replay_case


def numpy_acf(values, max_lag):
    values = np.asarray(values, dtype=float)
    z = values-values.mean()
    energy = float(z @ z)
    return (np.asarray([z[:len(z)-k] @ z[k:] / energy for k in range(max_lag+1)])
            if energy else np.zeros(max_lag+1))


class ACFRpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case, cls.metadata = load_snapshot()
        cls.results = {key: replay_case(cls.case, cfg) for key, cfg in SETTINGS.items()}

    def test_snapshot_is_the_selected_db_series_and_original_15_second_grid(self):
        self.assertEqual(self.metadata['measurement'], 'rps')
        self.assertEqual(self.metadata['tags'], {'replay': 'cusum-real-system-image'})
        self.assertTrue(self.metadata['grafana_frames_equal_influx'])
        self.assertTrue(self.metadata['read_only'])
        self.assertEqual(len(self.case.values), 5171)
        self.assertEqual(self.case.timestamps[0].isoformat(), '2026-09-22T10:00:00+00:00')
        self.assertEqual(self.case.timestamps[-1].isoformat(), '2026-09-23T07:32:30+00:00')
        self.assertTrue(all((b-a).total_seconds() == 15
                            for a, b in zip(self.case.timestamps, self.case.timestamps[1:])))
        self.assertAlmostEqual(float(self.case.values.min()), 482.738532, places=6)
        self.assertAlmostEqual(float(self.case.values.max()), 1009.966159, places=6)

    def test_replay_preserves_every_input_value_and_both_time_boundaries(self):
        for key, (rows, _, _) in self.results.items():
            with self.subTest(config=key):
                self.assertEqual(len(rows), len(self.case.values))
                np.testing.assert_array_equal([r['value'] for r in rows], self.case.values)
                self.assertEqual(rows[0]['timestamp_utc'], self.metadata['actual_start_utc'])
                self.assertEqual(rows[-1]['timestamp_utc'], self.metadata['actual_end_utc'])
                self.assertEqual(rows[-1]['index'], 5170)

    def test_prefix_training_and_warmup_are_separate_and_results_bounded(self):
        for key, cfg in SETTINGS.items():
            with self.subTest(config=key):
                rows = self.results[key][0]
                first = cfg['reference_window']+cfg['window']-1
                self.assertTrue(all(not r['ready'] and r['score'] == 0 for r in rows[:first]))
                self.assertTrue(all(r['ready'] for r in rows[first:]))
                self.assertEqual(rows[first]['phase'], 'scored')
                self.assertTrue(all(math.isfinite(r['score']) and 0 <= r['score'] <= 1 for r in rows))
                self.assertTrue(all(0 <= r['depth'] <= cfg['max_lag'] for r in rows))

    def test_acf_bands_score_and_depth_match_independent_numpy_at_peak(self):
        for key, cfg in SETTINGS.items():
            with self.subTest(config=key):
                _, result, index = self.results[key]
                local = numpy_acf(self.case.values[index-cfg['window']+1:index+1], cfg['max_lag'])
                healthy = numpy_acf(self.case.values[:cfg['reference_window']], cfg['max_lag'])
                bands = [0.0]+[min(2, max(cfg['min_change'], cfg['threshold']*
                         math.sqrt((1+2*np.dot(healthy[1:k], healthy[1:k]))*
                                   (1/cfg['window']+1/cfg['reference_window']))))
                                    for k in range(1, cfg['max_lag']+1)]
                np.testing.assert_allclose(result.acf, local, atol=1e-10)
                np.testing.assert_allclose(result.reference_acf, healthy, atol=1e-10)
                np.testing.assert_allclose(result.thresholds, bands, atol=1e-10)
                changes = np.abs(local-healthy)
                significant = tuple(k for k in range(1, cfg['max_lag']+1) if changes[k] > bands[k])
                expected = sum(min(1, changes[k]-bands[k]) for k in significant)/cfg['max_lag']
                self.assertAlmostEqual(result.score, expected, places=10)
                self.assertEqual(result.depth, max(significant, default=0))

    def test_deep_rps_dip_does_not_become_a_large_persistence_score(self):
        # The selected RPS trace has a very deep dip, but little change in its
        # normalized lag profile. Preserve that observed distinction from severity.
        for key, (rows, result, _) in self.results.items():
            with self.subTest(config=key):
                self.assertGreater(result.score, 0)
                self.assertLess(result.score, 0.1)
                ready = [r for r in rows if r['ready']]
                self.assertLess(sum(r['score'] > 0 for r in ready)/len(ready), 0.01)

    def test_future_values_do_not_change_scores_before_the_dip(self):
        stop = int(np.argmin(self.case.values))+10
        prefix = replace(self.case, values=self.case.values[:stop], timestamps=self.case.timestamps[:stop])
        for key, cfg in SETTINGS.items():
            with self.subTest(config=key):
                prefix_rows = replay_case(prefix, cfg)[0]
                self.assertEqual(prefix_rows, self.results[key][0][:stop])


if __name__ == '__main__':
    unittest.main()
