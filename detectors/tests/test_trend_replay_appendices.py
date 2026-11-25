import csv
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from apps.detectors import TrendDetector
from tests.test_trend_change_report import fixture
from tests.trend_replay_appendices import (
    CONFIG, FIXTURES, SNAPSHOT, group_points, parse_flux_csv, replay, screenshot_fixture, screenshot_metadata,
)
from tests.visualize_trend_change import append_replays, create_report


class TrendReplayAppendixTests(unittest.TestCase):
    def test_designed_teeth_are_one_sample_and_timestamps_are_fifteen_seconds(self):
        values,times,phases,teeth,background=screenshot_fixture()
        changed=np.flatnonzero(np.abs(values-background)>1e-6)
        np.testing.assert_array_equal(changed,teeth)
        self.assertEqual(len(teeth),22)
        self.assertTrue(all((b-a).total_seconds()==15 for a,b in zip(times,times[1:])))
        self.assertTrue(all(values[i+1]==background[i+1] for i in teeth))
        self.assertEqual(phases[-1]['end'],len(values))
        self.assertEqual(float(np.max(np.abs(values[-300:]))),0)
        self.assertTrue(np.any(values[teeth]>background[teeth]))
        self.assertTrue(np.any(values[teeth]<background[teeth]))

    def test_corrected_screenshot_has_two_step_episodes_and_quiet_second_high_plateau(self):
        values,times,phases,peaks,background=screenshot_fixture()
        metadata=screenshot_metadata()
        self.assertEqual(len(values),3956)
        self.assertEqual(hashlib.sha256((FIXTURES/metadata['source_image']).read_bytes()).hexdigest(),metadata['source_image_sha256'])
        steps=[s['index'] for s in metadata['main_steps']]
        self.assertEqual(steps,[912,1416,1974,2284,2766,3322])
        expected=[0,100,167,0,100,167,0]
        for a,b,level in zip([0,*steps],[*steps,len(values)],expected):
            self.assertAlmostEqual(float(np.median(background[a+10:b-10])),level,delta=1)
        self.assertFalse(any(phases[6]['start']<=i<phases[6]['end'] for i in peaks))
        # The photo's irregular peaks replace the former invented 20-point cycle.
        first=peaks[(peaks>=phases[2]['start'])&(peaks<phases[2]['end'])]
        self.assertGreater(len(set(np.diff(first))),1)

    def test_appendix_replay_matches_public_api_and_is_causal(self):
        values,times,_,_,_=screenshot_fixture()
        rows=replay(values,times)
        d=TrendDetector(**CONFIG)
        for row,value in zip(rows,values):
            result=d.update(float(value))
            self.assertEqual(row['score'],result.score)
            self.assertEqual(row['detected'],result.detected)
            self.assertEqual(row['change_start'],result.change_start)
        self.assertEqual(replay(values[:900],times[:900]),rows[:900])

    def test_flux_repeated_headers_and_frozen_snapshot_preserve_all_values(self):
        text=(',result,table,_time,_value,host\n,,0,2026-10-01T09:30:00Z,1,cassandra-1\n\n'
              '#datatype,string,long,string,double,string\n,result,table,host,_value,_time\n'
              ',,1,cassandra-2,2,2026-10-01T09:30:00Z\n')
        self.assertEqual([r['value_ms'] for r in parse_flux_csv(text)],[1.,2.])
        raw=parse_flux_csv((FIXTURES/(SNAPSHOT+'-raw.csv')).read_text(encoding='utf-8'))
        with (FIXTURES/(SNAPSHOT+'.csv')).open(encoding='utf-8',newline='') as stream:
            stored=[{**r,'value_ms':float(r['value_ms'])} for r in csv.DictReader(stream)]
        self.assertEqual(raw,stored)
        groups=group_points(raw)
        self.assertEqual({k:len(v) for k,v in groups.items()},{f'cassandra-{i}':5761 for i in range(1,5)})

    def test_appending_keeps_base_results_and_exports_real_values_decisions_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            original=create_report(root,scenarios=[fixture()],mc_seeds=0,with_benchmark=False)
            report=append_replays(root)
            for key in ('scenarios','groups','benchmark','test_run','total_points','scenario_count'):
                self.assertEqual(report[key],original[key])
            self.assertEqual(json.loads((root/'summary.json').read_text(encoding='utf-8')),report)
            self.assertEqual(len(report['appendices']['influx']['hosts']),4)
            for host in report['appendices']['influx']['hosts']:
                with (root/(host['stem']+'-points.csv')).open(encoding='utf-8',newline='') as stream:
                    rows=list(csv.DictReader(stream))
                self.assertEqual(len(rows),5761)
                self.assertEqual(rows[-1]['timestamp_utc'],'2026-10-02T09:30:00Z')
                d=TrendDetector(**CONFIG)
                for row in rows:
                    r=d.update(float(row['value']))
                    self.assertEqual(float(row['score']),r.score)
                    if row['change_start']:
                        self.assertLessEqual(int(row['change_start']),int(row['confirmed_at']))
                        self.assertLessEqual(int(row['confirmed_at']),int(row['index']))
            missing=[]
            ids=[]
            class Links(HTMLParser):
                def handle_starttag(self,tag,attrs):
                    for key,value in attrs:
                        if key=='id' and tag=='section':
                            ids.append(value)
                        if key in ('href','src') and value and not value.startswith(('#','http')) and not (root/value).is_file():
                            missing.append(value)
            Links().feed((root/'report.html').read_text(encoding='utf-8'))
            self.assertEqual(missing,[])
            self.assertEqual(ids[-6:],['screenshot-15s','influx-cassandra',*[f'influx-cassandra-{i}' for i in range(1,5)]])


if __name__=='__main__':
    unittest.main()
