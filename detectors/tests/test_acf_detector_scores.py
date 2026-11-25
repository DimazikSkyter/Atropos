"""Verify data identity, availability-time alignment and upper ACF independently."""
from dataclasses import replace
from datetime import datetime
import copy
import csv
import json
import math
import unittest

import numpy as np

from apps.detectors import ACFDetector,CusumDetector,TrendDetector,CorrelationDetector
from tests.acf_detector_scores_report import (FIXTURES,PROFILES,load_traces,read_csv,
                                             replay_acf,describe,parse_time)


class ACFDetectorScoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.traces,cls.manifest=load_traces()
        cls.by_name={t.name:t for t in cls.traces}
        cls.results={t.name:{key:replay_acf(t,cfg) for key,cfg in PROFILES.items()} for t in cls.traces}

    def test_exact_saved_inputs_and_uniform_available_grid(self):
        self.assertEqual(len(self.traces),5)
        expected={'cusum-orange-default':(3956,3927),'cusum-orange-wide':(3956,3897),
                  'correlation-latency':(112,107),'correlation-latency-ccf':(112,108),
                  'trend-cassandra-1':(5761,5522)}
        for trace in self.traces:
            self.assertEqual((len(trace.rows),len(trace.available)),expected[trace.name])
            self.assertTrue(np.all(np.isfinite(trace.values)))
            self.assertTrue(np.all((0<=trace.values)&(trace.values<=1)))
            self.assertTrue(all((b-a).total_seconds()==15 for a,b in zip(trace.times,trace.times[1:])))

    def test_cusum_scores_match_current_detectors_point_for_point(self):
        for key in ('default','wide'):
            trace=self.by_name['cusum-orange-'+key]
            detector=CusumDetector(**trace.config)
            rows=read_csv(FIXTURES/trace.input_file)
            observed=[detector.update(float(r['value'])) for r in rows]
            np.testing.assert_array_equal(observed,[float(r['score']) for r in rows])

    def test_trend_scores_match_current_detector_and_not_point_evidence(self):
        trace=self.by_name['trend-cassandra-1']
        detector=TrendDetector(**trace.config)
        rows=read_csv(FIXTURES/trace.input_file)
        observed=[detector.update(float(r['value'])).score for r in rows]
        np.testing.assert_array_equal(observed,[float(r['score']) for r in rows])
        self.assertTrue(any(float(r['score'])!=float(r['evidence']) for r in rows))

    def test_both_correlation_configs_replay_saved_scores_without_network(self):
        raw=read_csv(FIXTURES/'latency-input.csv')
        data=np.array([[float(r[k]) for k in ('blue','green','yellow')] for r in raw])
        for name in ('correlation-latency','correlation-latency-ccf'):
            trace=self.by_name[name]
            replay=CorrelationDetector(**trace.config).replay(data)
            by_index={r.score_index:r for r in replay}
            for record in trace.available:
                result=by_index[record['source_index']]
                self.assertTrue(result.ready)
                self.assertEqual(result.score,record['score'])
                self.assertEqual(parse_time(record['issued_at']),parse_time(raw[result.observed_index]['timestamp']))

    def test_latency_uses_issue_time_and_keeps_pending_out_of_acf(self):
        trace=self.by_name['correlation-latency']
        self.assertEqual(trace.available[0]['source_index'],4)
        for r in trace.available:
            self.assertEqual((parse_time(r['issued_at'])-parse_time(r['score_at'])).total_seconds(),15)
        self.assertEqual(sum(r['lower_provisional'] for r in trace.available),7)
        self.assertTrue(trace.rows[-1]['lower_pending'])
        self.assertIsNone(trace.rows[-1]['score'])
        self.assertNotIn(trace.rows[-1],trace.available)

    def test_lower_warmup_zeros_do_not_train_upper_reference(self):
        for trace in self.traces:
            for key,cfg in PROFILES.items():
                rows,_=self.results[trace.name][key]
                self.assertEqual(len(rows),len(trace.available))
                for i,r in enumerate(rows):
                    self.assertEqual(r['upper_index'],i)
                    self.assertEqual(r['source_index'],trace.available[i]['source_index'])
                    self.assertEqual(r['score'],trace.available[i]['score'])
                    self.assertEqual(r['acf_ready'],i>=cfg['reference_window']+cfg['window']-1)
                    if not r['acf_ready']:
                        self.assertIsNone(r['acf_score'])
                        self.assertIsNone(r['depth'])

    def test_latency_does_not_fabricate_ready_results_for_long_profiles(self):
        trace=self.by_name['correlation-latency']
        for key in ('default','short'):
            rows,snapshots=self.results[trace.name][key]
            summary=describe(trace,PROFILES[key],rows,snapshots)
            self.assertEqual(summary['ready_points'],0)
            self.assertIsNone(summary['max_score'])
            self.assertIsNone(summary['max_depth'])
        for key in ('compact','short_reference'):
            self.assertTrue(any(r['acf_ready'] for r in self.results[trace.name][key][0]))

    def test_numpy_oracle_for_profiles_bands_scores_and_depth_at_peaks(self):
        for trace in self.traces:
            values=trace.values
            for key,cfg in PROFILES.items():
                rows,snapshots=self.results[trace.name][key]
                valid=[i for i,r in enumerate(rows) if r['acf_ready']]
                if not valid:continue
                peak=max(valid,key=lambda i:rows[i]['acf_score'])
                ref=values[:cfg['reference_window']]
                def profile(x):
                    z=x-x.mean();e=z@z
                    return np.array([float(z[k:]@z[:len(z)-k]/e) if e else 0.
                                     for k in range(cfg['max_lag']+1)])
                background=profile(ref)
                for i in (valid[0],peak,valid[-1]):
                    current=profile(values[i-cfg['window']+1:i+1])
                    bands=np.array([0.]+[min(2.,max(cfg['min_change'],cfg['threshold']*math.sqrt(
                        (1+2*np.sum(background[1:k]**2))*(1/cfg['window']+1/len(ref)))))
                        for k in range(1,cfg['max_lag']+1)])
                    diff=abs(current-background)
                    significant=[k for k in range(1,cfg['max_lag']+1) if diff[k]>bands[k]]
                    score=sum(min(1.,max(0.,diff[k]-bands[k])) for k in range(1,cfg['max_lag']+1))/cfg['max_lag']
                    r=snapshots[i]
                    np.testing.assert_allclose(r.acf,current,atol=1e-13)
                    np.testing.assert_allclose(r.reference_acf,background,atol=1e-13)
                    np.testing.assert_allclose(r.thresholds,bands,atol=1e-13)
                    self.assertAlmostEqual(r.score,score,places=13)
                    self.assertEqual(r.depth,max(significant,default=0))

    def test_upper_stream_is_causal_and_preserves_final_available_score(self):
        trace=self.by_name['cusum-orange-wide']
        prefix=replace(trace,available=trace.available[:1100])
        for key,cfg in PROFILES.items():
            a,_=replay_acf(prefix,cfg);b,_=self.results[trace.name][key]
            self.assertEqual(a,b[:1100])
            self.assertEqual(b[-1]['source_index'],len(trace.rows)-1)
            self.assertEqual(b[-1]['score'],trace.available[-1]['score'])

    def test_correlation_pending_cannot_leak_future_into_upper_outputs(self):
        trace=self.by_name['correlation-latency']
        raw=read_csv(FIXTURES/'latency-input.csv')
        values=np.array([[float(r[k]) for k in ('blue','green','yellow')] for r in raw])
        prefix=CorrelationDetector(**trace.config).replay(values[:80])
        full=CorrelationDetector(**trace.config).replay(values)
        issued=lambda rows:[(r.score_index,r.observed_index,r.score) for r in rows
                            if r.ready and r.observed_index is not None and r.observed_index<80]
        self.assertEqual(issued(prefix),issued(full))

    def test_flat_saturated_score_is_forgotten_after_current_window(self):
        d=ACFDetector(**PROFILES['short']).fit([0.]*160)
        for v in [0.]*40:d.update(v)
        entering=[d.update(1.) for _ in range(40)]
        self.assertGreater(max(entering),0)
        self.assertEqual(entering[-1],0)
        self.assertTrue(d.result.zero_energy)
        self.assertEqual(d.result.depth,0)

    def test_all_ready_outputs_are_finite_and_depth_uses_15_second_grid(self):
        for trace in self.traces:
            for key,cfg in PROFILES.items():
                rows,_=self.results[trace.name][key]
                for r in rows:
                    if not r['acf_ready']:continue
                    self.assertTrue(math.isfinite(r['acf_score']) and 0<=r['acf_score']<=1)
                    self.assertTrue(0<=r['depth']<=cfg['max_lag'])
                    self.assertEqual(r['depth_seconds'],15*r['depth'])
                    self.assertEqual(r['horizon_reached'],r['depth']==cfg['max_lag'])


if __name__=='__main__':unittest.main(verbosity=2)
