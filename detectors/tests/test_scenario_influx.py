"""Regression coverage for the corrected live snapshot, with no live requests."""
from datetime import datetime
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tests.acf_replay_scenarios import replay_scenarios
from tests.scenario_influx import FIXTURES, STEM, TRACES, load_snapshot, validate_points
from tests.test_acf_replays import INPUT_HASHES


class ScenarioSnapshotTests(unittest.TestCase):
    def test_snapshot_matches_the_four_live_influx_fingerprints(self):
        groups, meta = load_snapshot()
        self.assertTrue(meta['read_only'])
        self.assertTrue(meta['grafana_frames_equal_influx'])
        self.assertEqual(meta['measurement'], 'screenshot_step_replay')
        self.assertEqual(meta['unit'], 'TPS')
        self.assertEqual(meta['points'], 15824)
        self.assertEqual(set(groups), set(TRACES))
        cases = {c.name: c for c in replay_scenarios()}
        self.assertNotIn('screenshot-15s', cases)
        for trace, (times, values) in groups.items():
            with self.subTest(trace=trace):
                self.assertEqual(len(values), 3956)
                self.assertEqual(times[0].isoformat(), '2026-10-17T11:23:30+00:00')
                self.assertEqual(times[-1].isoformat(), '2026-10-18T03:52:15+00:00')
                self.assertEqual({(b-a).total_seconds() for a,b in zip(times,times[1:])},{15.0})
                name = 'influx-scenario-'+trace
                self.assertEqual(hashlib.sha256(values.astype('<f8').tobytes()).hexdigest(), INPUT_HASHES[name])
                np.testing.assert_array_equal(cases[name].values, values)
                self.assertEqual(cases[name].timestamps, times)

    def test_seven_rollbacks_keep_their_width_and_sloped_shoulders(self):
        groups, _ = load_snapshot()
        times, values = groups['orange']
        windows = [('20:36','20:42'),('20:48','20:55'),('21:00','21:07'),('21:12','21:19'),
                   ('21:23','21:30'),('21:36','21:41'),('21:46','21:52')]
        widths=[]
        for left,right in windows:
            start=datetime.fromisoformat('2026-10-17T'+left+':00+03:00')
            end=datetime.fromisoformat('2026-10-17T'+right+':00+03:00')
            indices=[i for i,t in enumerate(times) if start <= t < end]
            i=indices[int(np.argmin(values[indices]))]
            a=b=i
            while values[a-1]<164.8:a-=1
            while values[b+1]<164.8:b+=1
            widths.append(b-a+1)
            self.assertLess(values[i],164)
            self.assertGreater(values[i-2],values[i])
            self.assertGreater(values[i+2],values[i])
        self.assertEqual(widths,[11,9,10,12,9,5,14])

    def test_readers_and_replay_scenarios_do_not_access_the_network(self):
        with patch('http.client.HTTPConnection',side_effect=AssertionError('Offline test made a request')):
            self.assertEqual(len(load_snapshot()[0]),4)
            self.assertEqual(len(list(replay_scenarios())),8)

    def test_corrupted_csv_is_rejected_before_replaying(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for suffix in ['.csv','.json']:
                shutil.copy2(FIXTURES/(STEM+suffix),root/(STEM+suffix))
            with (root/(STEM+'.csv')).open('a',encoding='utf-8') as f:
                f.write('\n')
            with self.assertRaisesRegex(ValueError,'checksum'):
                load_snapshot(root)

    def test_duplicate_or_missing_timestamps_are_rejected(self):
        groups,_=load_snapshot()
        points=[(s,t,float(v)) for s,(times,values) in groups.items() for t,v in zip(times,values)]
        points[1]=points[0]
        with self.assertRaisesRegex(ValueError,'unique timestamps'):
            validate_points(points)


if __name__=='__main__':
    unittest.main()
