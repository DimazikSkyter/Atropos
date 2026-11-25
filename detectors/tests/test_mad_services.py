"""Same service inputs as ACF, independently checked against the MAD definition."""

from dataclasses import replace
import math
import unittest

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from apps.detectors import MadDetector
from tests.acf_service_scenarios import load_snapshots
from tests.visualize_mad_services import CONFIG, replay_mad


class MadServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases, cls.metadata = load_snapshots()
        cls.results = {case.name: replay_mad(case) for case in cls.cases}

    def test_same_four_service_inputs_boundaries_warmup_and_final_point_as_acf(self):
        self.assertEqual(self.metadata['total_points'], 11364)
        self.assertEqual(len(self.cases), 4)
        for case in self.cases:
            with self.subTest(case=case.name):
                rows = self.results[case.name]
                self.assertEqual(len(rows), 2841)
                np.testing.assert_array_equal([r['value_ms'] for r in rows], case.values)
                self.assertEqual(rows[0]['timestamp_msk'], '2026-10-05T00:00:00+03:00')
                self.assertEqual(rows[-1]['timestamp_msk'], '2026-10-05T11:50:00+03:00')
                self.assertEqual(rows[-1]['index'], 2840)
                self.assertTrue(all(not r['ready'] and r['score'] == 0 for r in rows[:99]))
                self.assertTrue(all(r['ready'] for r in rows[99:]))
                self.assertEqual(rows[99]['timestamp_msk'], '2026-10-05T00:24:45+03:00')
                self.assertTrue(all(math.isfinite(r['score']) and 0 <= r['score'] <= 1 for r in rows))

    def test_every_ready_local_reference_and_score_match_independent_numpy(self):
        for case in self.cases:
            with self.subTest(case=case.name):
                reference = sliding_window_view(case.values, CONFIG['data_max_size'])
                local = reference[:, -CONFIG['window']:]
                ref_mad = np.mean(np.abs(reference-reference.mean(axis=1, keepdims=True)), axis=1)
                local_mad = np.mean(np.abs(local-local.mean(axis=1, keepdims=True)), axis=1)
                expected = np.tanh(.5*np.abs(local_mad-ref_mad)/ref_mad)
                rows = self.results[case.name][99:]
                np.testing.assert_allclose([r['reference_mad_ms'] for r in rows], ref_mad, atol=1e-12)
                np.testing.assert_allclose([r['local_mad_ms'] for r in rows], local_mad, atol=1e-12)
                np.testing.assert_allclose([r['score'] for r in rows], expected, atol=1e-12)

    def test_service_replay_matches_batch_and_does_not_use_future_observations(self):
        for case in self.cases:
            with self.subTest(case=case.name):
                expected = [r['score'] for r in self.results[case.name]]
                self.assertEqual(MadDetector(**CONFIG).detect_single_seria(case.values), expected)
                prefix = replace(case, values=case.values[:1500], timestamps=case.timestamps[:1500])
                self.assertEqual(replay_mad(prefix), self.results[case.name][:1500])


if __name__ == '__main__':
    unittest.main()
