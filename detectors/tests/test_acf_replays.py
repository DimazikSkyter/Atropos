"""Frozen InfluxDB inputs, actual ACF semantics and causal windows."""

from dataclasses import replace
import hashlib
import math
import unittest

import numpy as np

from apps.detectors import ACFDetector
from tests.acf_replay_scenarios import SETTINGS, replay_scenarios
from tests.trend_replay_appendices import MSK
from tests.visualize_acf_replays import replay_case, summarize


INPUT_HASHES={
    'influx-scenario-orange':'d2d676d5ffcfb7446812d51bf7570e3906624136757d0d497759d7e7690ec8bd',
    'influx-scenario-green':'d97baafb9fd52fa85d0d269f912737b43c100f91a9fcec5304c45fc9d7dff87e',
    'influx-scenario-yellow':'3988ebadd74189d818b056745c445d1b1a1d39388770b15908c18759c707b98e',
    'influx-scenario-blue':'073e5223a3c76697ed9d390434861aeee6b78b530669851aabb1f34f52da9228',
    'influx-cassandra-1':'bd5bccc6fc9c138ab74c769a03e39e332cc349bdc99125879f923b8d79cfe4be',
    'influx-cassandra-2':'2db0092145aa341c4d5b3edd68c893e722e8780a13559eebe4a9622124d32308',
    'influx-cassandra-3':'4fa2c5c70e125ffb72f56291f7b08dbfca8c90b3d73b75dd01cf0683fa77bc6f',
    'influx-cassandra-4':'c6e03e98b2d8f18a1cd7ef74faccaa14c10d6c8f50b84cb9cb64fced33bb098f',
}


def direct_profile(values,lag):
    centered=np.asarray(values,dtype=float)-np.mean(values)
    energy=float(np.dot(centered,centered))
    if energy==0:
        return np.zeros(lag+1)
    return np.array([np.dot(centered[:len(centered)-k],centered[k:])/energy for k in range(lag+1)])


class ACFReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=list(replay_scenarios())
        cls.results={(c.name,key):replay_case(c,cfg) for c in cls.cases for key,cfg in SETTINGS.items()}

    def test_four_new_scenario_and_four_unchanged_cassandra_series(self):
        self.assertEqual({c.name for c in self.cases},set(INPUT_HASHES))
        for c in self.cases:
            with self.subTest(case=c.name):
                digest=hashlib.sha256(np.asarray(c.values,dtype='<f8').tobytes()).hexdigest()
                self.assertEqual(digest,INPUT_HASHES[c.name])
                self.assertEqual(len(c.values),3956 if c.name.startswith('influx-scenario-') else 5761)
                self.assertTrue(all((b-a).total_seconds()==15 for a,b in zip(c.timestamps,c.timestamps[1:])))

    def test_prefix_training_warmup_last_point_and_bounded_outputs(self):
        for c in self.cases:
            for key,cfg in SETTINGS.items():
                with self.subTest(case=c.name,settings=key):
                    rows,_,_=self.results[c.name,key]
                    first=cfg['reference_window']+cfg['window']-1
                    self.assertEqual(len(rows),len(c.values))
                    self.assertEqual(rows[-1]['index'],len(c.values)-1)
                    self.assertTrue(all(not r['ready'] and r['score']==0 for r in rows[:first]))
                    self.assertTrue(all(r['ready'] for r in rows[first:]))
                    self.assertEqual(rows[first]['phase'],'scored')
                    self.assertTrue(all(0<=r['score']<=1 and 0<=r['depth']<=cfg['max_lag'] for r in rows))
                    self.assertEqual(rows[-1]['timestamp_msk'],c.timestamps[-1].astimezone(MSK).isoformat())

    def test_acf_thresholds_score_and_depth_match_independent_numpy_at_peak(self):
        for c in self.cases:
            for key,cfg in SETTINGS.items():
                with self.subTest(case=c.name,settings=key):
                    _,peak,index=self.results[c.name,key]
                    rho=direct_profile(c.values[index-cfg['window']+1:index+1],cfg['max_lag'])
                    ref=direct_profile(c.values[:cfg['reference_window']],cfg['max_lag'])
                    np.testing.assert_allclose(peak.acf,rho,atol=1e-10)
                    np.testing.assert_allclose(peak.reference_acf,ref,atol=1e-10)
                    bands=[0.]
                    for lag in range(1,cfg['max_lag']+1):
                        allowance=math.sqrt((1+2*np.dot(ref[1:lag],ref[1:lag]))*(1/cfg['window']+1/cfg['reference_window']))
                        bands.append(min(2,max(cfg['min_change'],cfg['threshold']*allowance)))
                    np.testing.assert_allclose(peak.thresholds,bands,atol=1e-10)
                    changes=np.abs(rho-ref)
                    significant=tuple(k for k in range(1,cfg['max_lag']+1) if changes[k]>bands[k])
                    score=sum(min(1,changes[k]-bands[k]) for k in significant)/cfg['max_lag']
                    self.assertAlmostEqual(peak.score,score,places=10)
                    self.assertEqual(peak.depth,significant[-1] if significant else 0)

    def test_later_values_do_not_change_previous_replay_results(self):
        for c in self.cases:
            prefix=replace(c,values=c.values[:950],timestamps=c.timestamps[:950])
            rows,_,_=replay_case(prefix,SETTINGS['default'])
            self.assertEqual(rows,self.results[c.name,'default'][0][:950])

    def test_step_episode_is_visible_and_idle_after_second_episode_recovers(self):
        c=self.cases[0]
        for key,cfg in SETTINGS.items():
            rows,peak,index=self.results[c.name,key]
            info=summarize(c,rows,peak,index,cfg)
            self.assertEqual(info['phases'][0]['nonzero_samples'],0)
            self.assertGreater(info['phases'][1]['max_score'],.05)
            end=c.phases[-1]['start']
            self.assertTrue(all(r['score']==0 for r in rows[end+cfg['window']:]))

    def test_high_plateau_profile_agrees_with_stream_without_invented_period(self):
        c=self.cases[0]
        # The chosen window is on the first high plateau, with its entry step gone.
        index=c.phases[2]['end']-1
        for key,cfg in SETTINGS.items():
            detector=ACFDetector(**cfg).fit(c.values[:cfg['reference_window']])
            result=detector.analyze(c.values[index-cfg['window']+1:index+1])
            row=self.results[c.name,key][0][index]
            self.assertEqual(result.score,row['score'])
            self.assertEqual(result.depth,row['depth'])
            self.assertEqual(result.depth,max(result.significant_lags,default=0))
            self.assertTrue(all(1<=lag<=cfg['max_lag'] for lag in result.significant_lags))
