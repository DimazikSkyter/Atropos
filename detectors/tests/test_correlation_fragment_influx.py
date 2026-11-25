"""Separate live experiment for the user-selected read_operations fragment."""
from datetime import datetime
import os
import unittest

import numpy as np

from apps.detectors import CorrelationDetector
from tests.correlation_change_replay import align_results
from tests.correlation_fragment_replay import CONFIG,START,END,load_fragment,region_summary


@unittest.skipUnless(os.getenv('RUN_INFLUX_INTEGRATION')=='1','Set RUN_INFLUX_INTEGRATION=1 to read Atropos InfluxDB')
class CorrelationFragmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case=load_fragment()

    def test_requested_fragment_contains_all_four_live_series_on_the_original_grid(self):
        case=self.case
        self.assertEqual(case.values.shape,(271,4))
        self.assertEqual(int(case.stamps[0]),int(datetime.fromisoformat(START).timestamp()))
        self.assertEqual(int(case.stamps[-1]),int(datetime.fromisoformat(END).timestamp()))
        self.assertEqual(set(np.diff(case.stamps)),{60})
        self.assertTrue(np.isfinite(case.values).all())

    def test_fresh_start_exposes_warmup_issue_time_and_pending_tail(self):
        rows=self.case.cold
        first=next(i for i,r in enumerate(rows) if r.ready)
        self.assertEqual(first,4)
        self.assertEqual(rows[first].observed_index,5)
        self.assertEqual(sum(r.ready for r in rows),266)
        self.assertEqual(sum(r.ready and r.provisional for r in rows),27)
        self.assertFalse(any(r.ready for r in rows[:4]))
        self.assertTrue(rows[-1].pending)
        for index,result in enumerate(rows):
            if result.ready:
                self.assertEqual(result.score_index,index)
                self.assertEqual(result.observed_index,index+1)
                self.assertTrue(0<=result.score<=1)

    def test_continuation_preserves_prior_state_and_the_same_observation_horizon(self):
        case=self.case
        self.assertEqual(case.start_index,55)
        self.assertEqual(len(case.prefix_values),326)
        self.assertEqual(int(case.prefix_stamps[-1]),int(case.stamps[-1]))
        self.assertTrue(case.continued[0].ready)
        self.assertEqual(case.continued[0].score_index,case.start_index)
        self.assertEqual(case.continued[0].observed_index,case.start_index+1)
        self.assertEqual(sum(r.ready for r in case.continued),270)
        self.assertFalse(any(r.provisional for r in case.continued if r.ready))
        self.assertTrue(case.continued[-1].pending)

    def test_fragment_prefix_scores_do_not_depend_on_unseen_later_points(self):
        case=self.case
        for stop in (45,141,230):
            with self.subTest(stop=stop):
                prefix=align_results(CorrelationDetector(**CONFIG).replay(case.values[:stop]),stop)
                self.assertEqual(prefix[:-1],case.cold[:stop-1])
                self.assertTrue(prefix[-1].pending)

    def test_continuation_has_no_alarm_on_the_visible_quiet_interval(self):
        result=region_summary(self.case,'continued','2026-10-18T19:40:00+03:00','2026-10-18T21:20:00+03:00')
        self.assertEqual(result['scored_points'],100)
        self.assertEqual(result['alarms'],0)

    def test_continuation_responds_to_both_visible_disagreements(self):
        case=self.case
        for start,stop,member in [('2026-10-18T21:30:00+03:00','2026-10-18T22:15:00+03:00',2),
                                  ('2026-10-18T23:25:00+03:00','2026-10-19T00:10:00+03:00',3)]:
            with self.subTest(start=start):
                rows=[r for t,r in zip(case.stamps,case.continued) if r.ready and
                      datetime.fromisoformat(start).timestamp()<=t<datetime.fromisoformat(stop).timestamp()]
                self.assertTrue(any(r.detected and member in r.affected_series for r in rows))


if __name__=='__main__':
    unittest.main()
