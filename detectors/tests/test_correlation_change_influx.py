"""Explicit live regression: all screenshot samples come from InfluxDB queries.

Set RUN_INFLUX_INTEGRATION=1. Missing service/data FAILS enabled tests; there is
no local-CSV fallback. Pure algorithm tests live in test_correlation_change.
"""
from datetime import datetime
import os
import unittest

import numpy as np

from apps.detectors import CorrelationDetector
from tests.correlation_change_replay import SETTINGS, replay,align_results
from tests.influx_correlation_data import CASES, load_case


@unittest.skipUnless(os.getenv('RUN_INFLUX_INTEGRATION') == '1',
                     'Set RUN_INFLUX_INTEGRATION=1 to read Atropos InfluxDB')
class CorrelationInfluxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.replays = {name: replay(name) for name in CASES}

    def test_live_series_counts_dates_and_uniform_grid(self):
        for name,count,step in [('latency',112,15),('read_operations',987,60)]:
            with self.subTest(case=name):
                stamps,values,_,_ = self.replays[name]
                self.assertEqual(values.shape,(count,len(CASES[name])))
                self.assertEqual(set(np.diff(stamps)),{step})
                self.assertTrue(np.isfinite(values).all())
                self.assertGreaterEqual(stamps[0],datetime.fromisoformat('2026-10-18T00:00:00+03:00').timestamp())
                self.assertLess(stamps[-1],datetime.fromisoformat('2026-10-20T00:00:00+03:00').timestamp())

    def test_full_latency_score_starts_after_one_minute_and_includes_startup(self):
        stamps,values,split,results=self.replays['latency']
        self.assertEqual(split,0)
        self.assertEqual(len(results),len(values))
        first=next(i for i,r in enumerate(results) if r.ready)
        self.assertEqual(int(stamps[first]-stamps[0]),60)
        self.assertEqual(first,4)
        self.assertEqual(sum(r.ready for r in results),107)
        self.assertEqual(sum(r.ready and r.provisional for r in results),7)
        self.assertTrue(all(r.ready for r in results[first:-1]))
        self.assertTrue(results[-1].pending)
        self.assertFalse(results[-1].ready)
        self.assertEqual(int(stamps[results[first].observed_index]-stamps[first]),15)
        early=[r for t,r in zip(stamps,results) if r.ready and
               t<datetime.fromisoformat('2026-10-18T17:03:30+03:00').timestamp()]
        self.assertGreater(len(early),35)
        self.assertFalse(any(r.detected for r in early))
        self.assertTrue(any(r.score>0 for r in early))

    def test_latency_detects_yellow_divergence_and_clears_on_quiet_tail(self):
        stamps,_,split,results = self.replays['latency']
        event = [r for t,r in zip(stamps[split:],results)
                 if datetime.fromisoformat('2026-10-18T17:04:30+03:00').timestamp() <= t <
                    datetime.fromisoformat('2026-10-18T17:08:30+03:00').timestamp() and r.ready]
        self.assertTrue(event)
        self.assertGreater(max(r.score for r in event),.8)
        self.assertTrue(any(p.score>.8 and 2 in p.pair for r in event for p in r.pairs))
        tail = [r for t,r in zip(stamps[split:],results)
                if t>=datetime.fromisoformat('2026-10-18T17:16:00+03:00').timestamp()]
        self.assertTrue(tail)
        self.assertFalse(any(r.detected for r in tail))

    def test_read_operations_quiet_baseline_then_individual_spike_and_divergence(self):
        stamps,_,split,results = self.replays['read_operations']
        def segment(a,b):
            return [r for t,r in zip(stamps[split:],results) if r.ready and
                    datetime.fromisoformat(a).timestamp() <= t < datetime.fromisoformat(b).timestamp()]
        quiet = segment('2026-10-18T20:15:00+03:00','2026-10-18T21:20:00+03:00')
        self.assertGreater(len(quiet),50)
        self.assertFalse(any(r.detected for r in quiet))
        spike = segment('2026-10-18T21:30:00+03:00','2026-10-18T22:15:00+03:00')
        self.assertTrue(any(r.detected and 2 in r.affected_series for r in spike))
        split_region = segment('2026-10-18T23:30:00+03:00','2026-10-19T00:30:00+03:00')
        self.assertGreater(sum(r.detected for r in split_region),len(split_region)*.5)

    def test_common_changes_from_database_do_not_trigger_when_relationship_is_preserved(self):
        for name,(stamps,values,split,_) in self.replays.items():
            with self.subTest(case=name):
                # Use the actual screenshot signal shape, including its jumps.
                common = np.column_stack([values[:,0]+10*i for i in range(values.shape[1])])
                detector = CorrelationDetector(**SETTINGS[name])
                results = detector.replay(common)
                self.assertTrue(any(r.ready for r in results))
                self.assertEqual(max(r.score for r in results),0)

    def test_read_operations_small_early_difference_and_common_drop_do_not_alarm(self):
        stamps,_,_,rows=self.replays['read_operations']
        early=[r for t,r in zip(stamps,rows) if r.ready and
               datetime.fromisoformat('2026-10-18T19:00:00+03:00').timestamp()<=t<
               datetime.fromisoformat('2026-10-18T19:40:00+03:00').timestamp()]
        self.assertGreater(len(early),30)
        self.assertFalse(any(r.detected for r in early))
        drop=[r for t,r in zip(stamps,rows) if r.ready and
              datetime.fromisoformat('2026-10-18T19:28:00+03:00').timestamp()<=t<
              datetime.fromisoformat('2026-10-18T19:40:00+03:00').timestamp()]
        self.assertLess(max(r.score for r in drop),.1)

    def test_live_stream_batch_and_prefix_causality_match(self):
        _,values,split,expected = self.replays['latency']
        stream = CorrelationDetector(**SETTINGS['latency'])
        self.assertEqual(align_results([stream.update(row) for row in values],len(values)),expected)
        for stop in (5,11,12,20,len(values)-5):
            prefix = align_results(CorrelationDetector(**SETTINGS['latency']).replay(values[:stop]),stop)
            self.assertEqual(prefix[:-1],expected[:stop-1])
            self.assertTrue(prefix[-1].pending)


if __name__ == '__main__':
    unittest.main()
