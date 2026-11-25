"""Bounded scrape skew, delayed issue time, and preserved change sensitivity."""
import unittest
import numpy as np

from apps.detectors import CorrelationDetector as LevelCorrelationDetector
from tests.correlation_change_replay import align_results


def CorrelationDetector(*args,**kwargs):
    """Isolate temporal matching from the separately tested relative-level weight."""
    kwargs.setdefault('relative_change_scale',0.)
    return LevelCorrelationDetector(*args,**kwargs)


def edge_data(sign=1):
    rng=np.random.default_rng(81)
    t=np.arange(260)
    x=np.sin(t/20)+rng.normal(0,.05,len(t))
    x[100:]+=50
    x[180:]-=30
    y=x.copy()
    # Only a transition is sampled on different sides of the refresh boundary.
    y[100]=x[99]
    y[180]=x[179]
    return np.column_stack((x,sign*2*y+10))


class CorrelationAlignmentTests(unittest.TestCase):
    def test_one_point_scrape_skew_suppresses_common_edge_alarm(self):
        data=edge_data()
        config={'window':24,'max_lag':3,'reference_window':64,'persistence':2}
        strict=align_results(CorrelationDetector(**config).replay(data),len(data))
        tolerant=align_results(CorrelationDetector(**config,alignment_tolerance=1).replay(data),len(data))
        self.assertGreater(max(r.score for r in strict[99:125]),.4)
        self.assertLess(max(r.score for r in tolerant[99:125]),.1)
        self.assertLess(max(r.score for r in tolerant[179:205]),.1)
        self.assertTrue(any(p.alignment_shift!=0 for r in tolerant[99:125] for p in r.pairs))

    def test_negative_reference_is_matched_by_signed_coherence(self):
        data=edge_data(sign=-1)
        cfg={'window':24,'max_lag':3,'reference_window':64,'persistence':2,'alignment_tolerance':1}
        rows=align_results(CorrelationDetector(**cfg).replay(data),len(data))
        self.assertLess(max(r.score for r in rows[99:125]),.1)
        changed=data.copy()
        changed[210:,1]=2*changed[210:,0]+10
        rows=align_results(CorrelationDetector(**cfg).replay(changed),len(changed))
        self.assertGreater(max(r.score for r in rows[210:] if r.ready),.4)

    def test_delay_outside_tolerance_is_not_optimized_away(self):
        x=np.random.default_rng(7).normal(size=350)
        y=2*x+10
        y[150:]=2*x[147:-3]+10
        rows=align_results(CorrelationDetector(window=32,max_lag=4,alignment_tolerance=1,
                                              persistence=2).replay(np.column_stack((x,y))),len(x))
        self.assertGreater(max(r.score for r in rows[180:300]),.5)
        self.assertTrue(any(r.detected for r in rows[180:300]))

    def test_persistent_independent_series_still_break_the_relation(self):
        rng=np.random.default_rng(18)
        x=rng.normal(size=500)
        y=2*x+10
        y[200:]=rng.normal(size=300)
        results=CorrelationDetector(alignment_tolerance=1).replay(np.column_stack((x,y)))
        self.assertGreater(sum(r.detected for r in results[240:]),100)

    def test_output_names_target_and_issue_indices_and_leaves_tail_pending(self):
        data=edge_data()[:20]
        detector=CorrelationDetector(window=12,max_lag=2,alignment_tolerance=1)
        issued=detector.replay(data)
        first=next(r for r in issued if r.ready)
        self.assertEqual(first.score_index,4)
        self.assertEqual(first.observed_index,5)
        self.assertEqual(first.alignment_delay,1)
        rows=align_results(issued,len(data))
        self.assertEqual(sum(r.ready for r in rows),15)
        self.assertTrue(rows[-1].pending)
        self.assertFalse(rows[-1].ready)
        self.assertTrue(all(not r.pending for r in rows[:-1]))

    def test_stream_and_prefix_replay_match_at_issue_time(self):
        data=edge_data()[:120]
        detector=CorrelationDetector(alignment_tolerance=1)
        expected=detector.replay(data)
        self.assertEqual(expected,[detector.update(row) for row in data])
        for stop in (5,6,12,40,101):
            self.assertEqual(detector.replay(data[:stop]),expected[:stop])

    def test_right_halo_cannot_train_its_own_reference_or_change_earlier_outputs(self):
        rng=np.random.default_rng(23)
        x=rng.normal(size=100)
        data=np.column_stack((x,2*x+10))
        changed=data.copy()
        changed[75]=[1e6,-1e6]
        a,b=CorrelationDetector(alignment_tolerance=1),CorrelationDetector(alignment_tolerance=1)
        for index in range(76):
            ra,rb=a.update(data[index]),b.update(changed[index])
            if index<75:
                self.assertEqual(ra,rb)
        self.assertEqual(list(a._history),list(b._history))
        self.assertEqual(a._profiles,b._profiles)
        self.assertEqual(a._anchors,b._anchors)
        self.assertEqual(a._relations,b._relations)
        self.assertEqual(a.result.score_index,74)
        self.assertEqual(a.result.observed_index,75)

    def test_constructor_validation_and_zero_tolerance_has_no_extra_delay(self):
        for kwargs in ({'alignment_tolerance':-1},{'alignment_tolerance':True},
                       {'alignment_tolerance':1.5},{'alignment_tolerance':3},
                       {'adaptive':False,'alignment_tolerance':1}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                CorrelationDetector(**kwargs)
        results=CorrelationDetector(alignment_tolerance=0).replay(edge_data()[:20])
        first=next(r for r in results if r.ready)
        self.assertEqual(first.score_index,first.observed_index)
        self.assertEqual(first.alignment_delay,0)
        self.assertFalse(any(r.pending for r in results))

    def test_fit_reset_preserves_alignment_configuration_and_clears_pending_rows(self):
        data=edge_data()[:80]
        detector=CorrelationDetector(window=12,max_lag=2,alignment_tolerance=1).fit(data[:30])
        first=detector.replay(data[30:])
        for row in data[30:50]:
            detector.update(row)
        detector.reset()
        self.assertEqual(detector.alignment_tolerance,1)
        self.assertEqual(len(detector._pending),0)
        self.assertEqual(first,[detector.update(row) for row in data[30:]])

    def test_tiny_accidental_prefix_cannot_establish_a_reference(self):
        x=np.arange(8,dtype=float)
        detector=CorrelationDetector(window=12,max_lag=2)
        for row in np.column_stack((x,2*x+10)):
            detector.update(row)
        self.assertTrue(detector.ready)
        self.assertFalse(detector.reference_ready)


if __name__=='__main__':
    unittest.main()
