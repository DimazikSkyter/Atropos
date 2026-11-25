"""Offline ACF regression on all four curves of the linked Grafana panel."""

from dataclasses import replace
import math
import unittest

import numpy as np

from tests.acf_replay_scenarios import SETTINGS
from tests.acf_service_scenarios import load_snapshots
from tests.visualize_acf_replays import replay_case


def direct_acf(values, max_lag):
    values = np.asarray(values, dtype=float)
    z = values-values.mean()
    energy = z @ z
    return (np.asarray([z[:len(z)-k] @ z[k:] / energy for k in range(max_lag+1)])
            if energy else np.zeros(max_lag+1))


class ACFServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases, cls.metadata = load_snapshots()
        cls.results = {(case.name, key): replay_case(case, cfg)
                       for case in cls.cases for key, cfg in SETTINGS.items()}

    def test_all_four_services_match_panel_range_units_and_15_second_grid(self):
        self.assertEqual(self.metadata['dashboard_uid'], 'service-response-time-20261005')
        self.assertEqual(self.metadata['panel_id'], 1)
        self.assertEqual(self.metadata['units'], 'ms')
        self.assertTrue(self.metadata['grafana_frames_equal_influx'])
        self.assertTrue(self.metadata['read_only'])
        self.assertEqual(self.metadata['total_points'], 11364)
        self.assertEqual({c.name for c in self.cases}, {f'service{i}-20261005' for i in range(1, 5)})
        for case in self.cases:
            with self.subTest(case=case.name):
                self.assertEqual(len(case.values), 2841)
                self.assertEqual(case.timestamps[0].isoformat(), '2026-10-04T21:00:00+00:00')
                self.assertEqual(case.timestamps[-1].isoformat(), '2026-10-05T08:50:00+00:00')
                self.assertTrue(all((b-a).total_seconds() == 15
                                    for a, b in zip(case.timestamps, case.timestamps[1:])))

    def test_replay_preserves_every_value_and_the_final_observation(self):
        for case in self.cases:
            for key in SETTINGS:
                with self.subTest(case=case.name, config=key):
                    rows = self.results[case.name, key][0]
                    self.assertEqual(len(rows), len(case.values))
                    np.testing.assert_array_equal([r['value'] for r in rows], case.values)
                    self.assertEqual(rows[0]['timestamp_utc'], '2026-10-04T21:00:00Z')
                    self.assertEqual(rows[-1]['timestamp_utc'], '2026-10-05T08:50:00Z')
                    self.assertEqual(rows[-1]['index'], 2840)

    def test_warmup_is_causal_and_all_outputs_are_finite_and_bounded(self):
        for case in self.cases:
            for key, cfg in SETTINGS.items():
                with self.subTest(case=case.name, config=key):
                    rows = self.results[case.name, key][0]
                    first = cfg['reference_window']+cfg['window']-1
                    self.assertTrue(all(not r['ready'] and r['score'] == 0 for r in rows[:first]))
                    self.assertTrue(all(r['ready'] for r in rows[first:]))
                    self.assertTrue(all(math.isfinite(r['score']) and 0 <= r['score'] <= 1 for r in rows))
                    self.assertTrue(all(0 <= r['depth'] <= cfg['max_lag'] for r in rows))
                    self.assertEqual(rows[first]['phase'], 'scored')

    def test_acf_thresholds_and_scores_match_independent_numpy_at_peak(self):
        for case in self.cases:
            for key, cfg in SETTINGS.items():
                with self.subTest(case=case.name, config=key):
                    _, result, index = self.results[case.name, key]
                    local = direct_acf(case.values[index-cfg['window']+1:index+1], cfg['max_lag'])
                    healthy = direct_acf(case.values[:cfg['reference_window']], cfg['max_lag'])
                    bands = [0]+[min(2, max(cfg['min_change'], cfg['threshold']*
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

    def test_appending_later_service_data_does_not_change_previous_decisions(self):
        for case in self.cases:
            prefix = replace(case, values=case.values[:950], timestamps=case.timestamps[:950])
            for key, cfg in SETTINGS.items():
                with self.subTest(case=case.name, config=key):
                    rows = replay_case(prefix, cfg)[0]
                    self.assertEqual(rows, self.results[case.name, key][0][:950])


if __name__ == '__main__':
    unittest.main()
