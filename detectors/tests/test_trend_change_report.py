import csv
from dataclasses import replace
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from apps.detectors import TrendDetector
from tests.trend_change_scenarios import Change, Scenario, inherited_scenarios
from tests.visualize_trend_change import aggregate, create_report, load_test_log, replay, summarize


def fixture():
    t=np.arange(500)
    return Scenario('test','Проверка отчёта',100+.2*np.maximum(t-250,0),
                    (Change(250,1,500),),group='change')


class TrendChangeReportTests(unittest.TestCase):
    def test_replay_public_results_and_future_independence(self):
        case=fixture()
        rows=replay(case)
        d=TrendDetector()
        for value,row in zip(case.values,rows):
            result=d.update(float(value))
            self.assertEqual(row['score'],result.score)
            self.assertEqual(row['change_start'],result.change_start)
            self.assertEqual(row['confirmed_at'],result.confirmed_at)
            self.assertEqual(row['horizontal'],result.horizontal)
        self.assertEqual(replay(replace(case,values=case.values[:280])),rows[:280])

    def test_confirmation_is_not_backdated_and_unknown_is_not_control(self):
        case=fixture()
        rows=replay(case)
        info=summarize(case,rows,'01')
        self.assertEqual(info['confirmed_events'],1)
        event=info['reactions'][0]
        self.assertTrue(event['detected'])
        self.assertEqual(event['estimated_start'],250)
        self.assertGreater(event['confirmed_at'],250)
        self.assertEqual(event['delay'],event['confirmed_at']-250)
        self.assertEqual(info['unmatched_events'],0)
        unknown=summarize(replace(case,changes=None),rows,'02')
        self.assertIsNone(unknown['labelled_changes'])
        self.assertIsNone(unknown['unmatched_events'])
        self.assertIsNone(aggregate([unknown])[0]['unmatched_events'])
        self.assertIsNone(aggregate([unknown])[0]['labelled_changes'])
        control=summarize(replace(case,changes=()),rows,'03')
        self.assertEqual(control['unmatched_events'],1)

    def test_wrong_direction_and_missed_events_remain_in_denominator(self):
        case=replace(fixture(),changes=(Change(250,-1,500),))
        info=summarize(case,replay(case),'01')
        self.assertEqual(info['detected_changes'],0)
        group=aggregate([info])[0]
        self.assertEqual(group['recall'],0)
        self.assertIsNone(group['onset_mae'])

    def test_inherited_data_are_identical_and_unlabelled(self):
        from tests.prediction_interval_scenarios import previous_report_scenarios, stability_scenarios
        originals=[*previous_report_scenarios(),*stability_scenarios(43)]
        copied=list(inherited_scenarios())
        self.assertEqual(len(copied),len(originals))
        for left,right in zip(originals,copied):
            np.testing.assert_array_equal(left.values,right.values)
            self.assertIsNone(right.changes)

    def test_report_links_json_csv_last_sample_and_test_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            log=root/'run.txt'
            log.write_text('Ran 15 tests in 1.25s\n\nOK (skipped=1)\n',encoding='utf-8')
            report=create_report(root,scenarios=[fixture()],mc_seeds=0,test_log=log,with_benchmark=False)
            self.assertEqual(json.loads((root/'summary.json').read_text(encoding='utf-8')),report)
            with (root/'0001-points.csv').open(encoding='utf-8',newline='') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual(len(rows),500)
            self.assertEqual(rows[-1]['index'],'499')
            self.assertEqual(rows[0]['horizontal'],'')
            self.assertEqual(report['test_run']['tests'],15)
            missing=[]
            class Links(HTMLParser):
                def handle_starttag(self,tag,attrs):
                    for key,value in attrs:
                        if key in ('href','src') and value and not value.startswith(('#','http')):
                            if not (root/value).is_file():
                                missing.append(value)
            Links().feed((root/'report.html').read_text(encoding='utf-8'))
            self.assertEqual(missing,[])
            self.assertTrue((root/'overview.png').is_file())
            log.write_text('Ran 2 tests in 1.0s\n\nFAILED (failures=1)',encoding='utf-8')
            with self.assertRaises(ValueError):
                load_test_log(log)


if __name__=='__main__':
    unittest.main()
