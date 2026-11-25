import csv
from dataclasses import asdict, replace
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from apps.runtime_trend import RuntimeTrendDetector, TrendConfig
from tests.prediction_interval_scenarios import previous_report_scenarios, stability_scenarios
from tests.runtime_trend_scenarios import Span, TrendScenario, legacy_scenarios, report_scenarios
from tests.visualize_runtime_trend import (
    create_report, expected_context, load_test_run, replay_scenario, summarize, window_diagnostics,
)


def small_case(**config):
    cfg = TrendConfig(120, 1, 0.5, **config)
    return TrendScenario('test-linear', 'linear', 'Линейный ряд',
                         tuple((i*15.0, 10+i*0.5) for i in range(11)), cfg,
                         'unit test', 'ед.', 'Рост', trends=(Span(0, 1000, 'Рост', 'up'),))


class RuntimeTrendReportTests(unittest.TestCase):
    def test_legacy_twelve_cases_keep_original_alarm_counts_and_public_results(self):
        counts = [0, 64, 8, 16, 0, 0, 0, 6, 54, 0, 0, 0]
        for case, count in zip(legacy_scenarios(), counts):
            with self.subTest(case=case.slug):
                _, rows = replay_scenario(case)
                self.assertEqual(sum(r['detected'] for r in rows), count)
                direct = RuntimeTrendDetector(case.config)
                expected = []
                for t, value in case.points:
                    direct.update(t, value)
                    if t % 60 == 0:
                        expected.append(asdict(direct.evaluate()))
                self.assertEqual([{k: row[k] for k in expected[0]} for row in rows], expected)
                self.assertTrue(summarize(case, rows, '01')['criterion_met'])

    def test_sixteen_imported_bond_signals_are_reused_without_changing_values(self):
        cases = report_scenarios()
        self.assertEqual(len(cases), 28)
        originals = {s.name: s for s in [*previous_report_scenarios(), *stability_scenarios(43)]}
        for case in cases[12:]:
            with self.subTest(case=case.slug):
                original = originals[case.slug]
                np.testing.assert_array_equal([v for _, v in case.points], original.values)
                np.testing.assert_array_equal([t for t, _ in case.points], np.arange(len(original.values))*15)

    def test_replay_keeps_unscheduled_points_blank_and_labels_final_evaluation(self):
        case = small_case(confirmation=2)
        samples, rows = replay_scenario(case)
        self.assertEqual(len(samples), 11)
        self.assertEqual([r['timestamp'] for r in rows], [0, 60, 120, 150])
        self.assertEqual(rows[-1]['evaluation_kind'], 'final')
        self.assertEqual(rows[-2]['reason'], 'confirmation')
        self.assertTrue(rows[-1]['detected'])
        self.assertNotIn('reason', samples[1])
        self.assertFalse(samples[1]['evaluated'])
        self.assertEqual(rows[-1]['samples'], 9)
        self.assertEqual(rows[-1]['window_start_seconds'], 30)
        self.assertEqual(rows[-1]['coverage'], 1)

    def test_past_evaluations_do_not_depend_on_future_samples(self):
        case = next(c for c in legacy_scenarios() if c.slug == 'response-quiet_rise')
        _, full = replay_scenario(case)
        _, prefix = replay_scenario(replace(case, points=case.points[:41]))
        self.assertEqual(prefix, [r for r in full if r['timestamp'] <= 600])

    def test_window_context_keeps_trend_memory_and_preserves_unknown_labels(self):
        trends = (Span(0, 60, 'Рост', 'up'),)
        self.assertEqual(expected_context(30, 90, trends), 'up')
        self.assertEqual(expected_context(60, 120, trends), 'steady')
        self.assertEqual(expected_context(0, 120, ()), 'steady')
        self.assertIsNone(expected_context(0, 120, None))

    def test_selected_fit_uses_actual_causal_window_and_correct_residual_units(self):
        case = small_case(value_scale=2)
        _, rows = replay_scenario(case)
        selected = rows[-1]
        window = window_diagnostics(case, selected)
        self.assertEqual(len(window), selected['samples'])
        self.assertEqual(window[0]['timestamp_seconds'], selected['window_start_seconds'])
        self.assertEqual(window[-1]['timestamp_seconds'], selected['timestamp'])
        self.assertTrue(all(r['timestamp_seconds'] <= selected['timestamp'] for r in window))
        np.testing.assert_allclose([r['fitted_value'] for r in window], [r['value'] for r in window])
        residual_second_moment = np.mean([r['winsorized_residual_normalized']**2 for r in window])
        self.assertAlmostEqual(residual_second_moment, selected['residual_variance'])
        self.assertAlmostEqual(selected['tangent'], 1)

    def test_complete_report_exports_valid_links_last_point_and_roundtrip_decisions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = create_report(root, scenarios=[small_case()])
            self.assertEqual(report['scenario_count'], 1)
            self.assertEqual(json.loads((root/'summary.json').read_text(encoding='utf-8')), report)
            with (root/'01-points.csv').open(encoding='utf-8', newline='') as stream:
                points = list(csv.DictReader(stream))
            self.assertEqual(len(points), 11)
            self.assertEqual(float(points[-1]['timestamp_seconds']), 150)
            self.assertEqual(points[-1]['evaluation_kind'], 'final')
            self.assertEqual(points[1]['tangent'], '')
            self.assertEqual(points[1]['reason'], '')
            with (root/'01-detections.csv').open(encoding='utf-8', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), report['scenarios'][0]['evaluations'])
            self.assertEqual(sum(r['detected']=='True' for r in rows), report['scenarios'][0]['alarm_evaluations'])
            missing = []
            class Links(HTMLParser):
                def handle_starttag(self, tag, attrs):
                    for key, value in attrs:
                        if key in ('src', 'href') and value and not value.startswith(('#', 'http')):
                            if not (root/value).is_file():
                                missing.append(value)
            Links().feed((root/'report.html').read_text(encoding='utf-8'))
            self.assertEqual(missing, [])
            self.assertEqual(len(list(root.glob('*.png'))), 3)

    def test_unready_window_is_explicit_and_does_not_invent_a_fit(self):
        case = replace(small_case(), points=((0, 1),))
        _, rows = replay_scenario(case)
        info = summarize(case, rows, '01')
        self.assertEqual(info['ready_evaluations'], 0)
        self.assertIsNone(info['max_abs_tangent'])
        self.assertIsNone(window_diagnostics(case, rows[0])[0]['fitted_value'])

    def test_test_log_validation_rejects_failed_and_accepts_complete_run(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder)/'tests.txt'
            log.write_text('Ran 5 tests in 1.25s\n\nFAILED (failures=1)\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                load_test_run(log)
            log.write_text('Ran 5 tests in 1.25s\n\nOK (skipped=1)\n', encoding='utf-8')
            metadata, _ = load_test_run(log)
            self.assertEqual(metadata, {'total': 5, 'seconds': 1.25, 'skipped': 1, 'status': 'OK'})


if __name__ == '__main__':
    unittest.main()
