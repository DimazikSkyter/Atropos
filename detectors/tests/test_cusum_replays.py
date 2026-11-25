"""Actual CUSUM response on the same corrected inputs as the other reports."""

from collections import deque
import csv
from dataclasses import replace
from html.parser import HTMLParser
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tests.acf_replay_scenarios import replay_scenarios
from tests.cusum_replay_report import SETTINGS,MARKER,append_recent,replay_case
from tests.test_acf_replays import INPUT_HASHES


class CusumReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.root=Path(cls.temp.name)
        cls.base=[{'scenario':'preserved-control','max_score':.125}]
        (cls.root/'summary.json').write_text(json.dumps(cls.base),encoding='utf-8')
        (cls.root/'report.html').write_text('<html><h1>Existing CUSUM report</h1></html>',encoding='utf-8')
        cls.meta=append_recent(cls.root)
        cls.cases=list(replay_scenarios())
        cls.rows={}
        for case in cls.meta['cases']:
            for key,v in case['variants'].items():
                with (cls.root/(v['stem']+'.csv')).open(encoding='utf-8',newline='') as stream:
                    cls.rows[case['name'],key]=list(csv.DictReader(stream))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_input_values_hashes_fifteen_second_grid_and_last_samples(self):
        self.assertEqual(self.meta['input_points'],38868)
        self.assertEqual(self.meta['variant_count'],16)
        for case in self.cases:
            self.assertEqual(hashlib.sha256(np.asarray(case.values,dtype='<f8').tobytes()).hexdigest(),INPUT_HASHES[case.name])
            for key in SETTINGS:
                rows=self.rows[case.name,key]
                np.testing.assert_array_equal([float(r['value']) for r in rows],case.values)
                self.assertEqual(int(rows[-1]['index']),len(case.values)-1)
                self.assertEqual([int(r['seconds']) for r in rows],list(range(0,len(rows)*15,15)))

    def test_exact_automatic_warmup_and_bounded_memory(self):
        for case in self.cases:
            for key,cfg in SETTINGS.items():
                rows=self.rows[case.name,key]
                first=20+cfg['window']-1
                self.assertTrue(all(r['ready']=='False' and float(r['score'])==0 for r in rows[:first]))
                self.assertTrue(all(r['ready']=='True' for r in rows[first:]))
                self.assertTrue(all(0<=float(r['score'])<=1 for r in rows))
                self.assertLessEqual(max(int(r['inertia_samples']) for r in rows),cfg['inertia_window'])

    def test_exported_scores_match_independent_window_cusums_and_finite_memory(self):
        # Uses reported public background estimates, independently deriving Q,
        # relative movement and the finite directional memory from raw inputs.
        for case in self.cases:
            for key,cfg in SETTINGS.items():
                rows=self.rows[case.name,key]
                memory=deque(maxlen=cfg['inertia_window'])
                for i,row in enumerate(rows):
                    if i<=20:continue
                    sigma=float(row['noise_std']);level=max(abs(float(row['background_level'])),sigma)
                    delta=case.values[i]-case.values[i-1]
                    movement=min(1e6,max(0.,abs(delta)-max(sigma,.01*level))/level)
                    memory.append((movement if delta>0 else 0.,movement if delta<0 else 0.))
                    if row['ready']=='False':continue
                    up=down=p=n=0.
                    for d in np.diff(case.values[i-cfg['window']+1:i+1]):
                        z=float(np.clip(d/sigma,-1e6,1e6))
                        up=max(0.,up+z-3);down=max(0.,down-z-3)
                        p=max(p,up);n=max(n,down)
                    q=-math.expm1(-p*n/25)
                    if p==0 and n==0:memory.clear()
                    u=sum(v[0] for v in memory);d=sum(v[1] for v in memory)
                    inertia=-math.expm1(-u*d/.25)
                    self.assertAlmostEqual(float(row['score']),q*inertia,places=10)
                    self.assertEqual(int(row['inertia_samples']),len(memory))

    def test_results_are_causal_on_prefixes(self):
        for case in self.cases:
            prefix=replace(case,values=case.values[:1000],timestamps=case.timestamps[:1000])
            for key,cfg in SETTINGS.items():
                rows=replay_case(prefix,cfg)
                expected=self.rows[case.name,key]
                np.testing.assert_array_equal([r['score'] for r in rows],[float(r['score']) for r in expected[:1000]])

    def test_idle_before_and_after_screenshot_episodes_is_quiet(self):
        case=self.cases[0]
        first=case.phases[1]['start'];end=case.phases[-1]['start']
        for key,cfg in SETTINGS.items():
            rows=self.rows[case.name,key]
            self.assertTrue(all(float(r['score'])==0 for r in rows[:first]))
            self.assertTrue(all(float(r['score'])==0 for r in rows[end+cfg['window']:]))
            self.assertGreater(max(float(r['score']) for r in rows[first:end]),.5)

    def test_report_preserves_base_and_repeated_append_does_not_duplicate(self):
        # Export generation above is real; reuse it to test the append operation
        # separately without repeating the numerical experiment or plots.
        with patch('tests.cusum_replay_report.create_appendix',return_value=self.meta):
            append_recent(self.root)
        result=json.loads((self.root/'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(result[0],self.base[0])
        self.assertEqual(len(result),9)
        page=(self.root/'report.html').read_text(encoding='utf-8')
        self.assertEqual(page.count(MARKER),1)
        missing=[]
        class Links(HTMLParser):
            def handle_starttag(self,tag,attrs):
                for k,v in attrs:
                    if k in ('src','href') and v and not v.startswith(('#','http')) and not (self_root/v).is_file():missing.append(v)
        self_root=self.root
        Links().feed(page)
        self.assertEqual(missing,[])
