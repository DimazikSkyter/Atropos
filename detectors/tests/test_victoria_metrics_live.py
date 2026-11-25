"""Opt-in live smoke test: validates API use/types, not current metric facts.

PowerShell:
  $env:RUN_VM_INTEGRATION='1'
  uv run python -m unittest tests.test_victoria_metrics_live -v
"""
from collections import Counter
import json
import os
import time
import unittest

from apps.metrics import MetricCatalog, MetricSeries, VictoriaMetricsClient


@unittest.skipUnless(os.getenv('RUN_VM_INTEGRATION') == '1', 'Set RUN_VM_INTEGRATION=1 for live VictoriaMetrics')
class VictoriaMetricsLiveTests(unittest.TestCase):
    def test_live_catalog_filters_and_recent_activity(self):
        client = VictoriaMetricsClient(os.getenv('VICTORIA_METRICS_URL', 'http://localhost:8428'))
        end = time.time()
        names = client.list_metric_names(end=end)
        tags = client.list_label_names(end=end)
        jobs = client.list_label_values('job', end=end)
        catalog = client.catalog(end=end)
        all_series = client.list_series(end=end)
        windows = catalog.filter(prefix='windows_')
        cpu = windows.filter(name_regex='.*cpu.*')
        selector = os.getenv('VM_TEST_SELECTOR', '{job="windows-exporter"}')
        activity = client.recent_activity(selector, lookback_seconds=120, end=end)
        active = client.active_catalog(selector, lookback_seconds=120, end=end)
        has_nonzero = client.has_recent_nonzero(selector, lookback_seconds=120, end=end)
        self.assertIsInstance(names, list)
        self.assertIsInstance(tags, list)
        self.assertIsInstance(jobs, list)
        self.assertIsInstance(catalog, MetricCatalog)
        self.assertIsInstance(active, MetricCatalog)
        self.assertIsInstance(has_nonzero, bool)
        for entry in all_series:
            self.assertIsInstance(entry, MetricSeries)
        for entry in activity:
            self.assertIsInstance(entry.has_data, bool)
            self.assertIsInstance(entry.has_nonzero, bool)
        # Empty catalogs, all-zero values, absent exporters are valid outcomes.
        # Connection/API failures fail this explicitly requested live test.
        print('\nVictoriaMetrics live smoke:', json.dumps({
            'metric_names': len(names), 'label_names': len(tags), 'jobs': jobs,
            'series': len(catalog.series), 'windows_series': len(windows.series),
            'cpu_series': len(cpu.series), 'recent_statuses': dict(Counter(s.status for s in activity)),
            'active_series': len(active.series), 'has_recent_nonzero': has_nonzero,
        }, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
