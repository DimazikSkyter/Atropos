import io
import json
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs

from apps.metrics import (
    MetricCatalog, MetricSeries, VictoriaMetricsClient, VictoriaMetricsError,
)


def series(name='latency', **tags):
    return MetricSeries.from_labels({'__name__': name, **tags})


class CatalogTests(unittest.TestCase):
    def test_filters_and_label_inventory(self):
        cpu = series('windows_cpu', job='windows', core='0')
        memory = series('windows_memory', job='windows')
        other = series('up', job='cadvisor')
        catalog = MetricCatalog((cpu, memory, other), 0, 100)
        self.assertEqual(catalog.filter(prefix='windows_', name_regex='.*cpu',
                                       labels={'job': 'windows'}, label_regex={'core': '\\d+'}).series, (cpu,))
        self.assertEqual(catalog.filter(names=['up', 'windows_cpu'], exclude_names=['up']).series, (cpu,))
        self.assertEqual(catalog.filter(names=[]).series, ())
        self.assertEqual(catalog.filter(labels={'absent': ''}).series, ())
        self.assertEqual(catalog.names, ['up', 'windows_cpu', 'windows_memory'])
        self.assertEqual(catalog.label_values('job'), ['cadvisor', 'windows'])
        self.assertEqual(catalog.label_values('missing'), [])
        self.assertIn('__name__', catalog.label_names)
        self.assertEqual(len(catalog.series), 3)

    def test_selector_escaping_and_label_copy(self):
        metric = series('latency', path='C:\\tmp\\"test"\n')
        self.assertIn('path=' + json.dumps(dict(metric.tags)['path']), metric.selector())
        labels = metric.labels
        labels['__name__'] = 'changed'
        self.assertEqual(metric.name, 'latency')


class ClientTests(unittest.TestCase):
    def test_activity_all_states_chunks_and_actual_time_range(self):
        client = VictoriaMetricsClient()
        entries = [series(name) for name in ('zero', 'nonzero', 'invalid', 'missing', 'tiny')]
        catalog = MetricCatalog(tuple(entries), 40, 100)
        def row(name, values, stamps):
            return {'metric': {'__name__': name}, 'values': values, 'timestamps': stamps}
        rows = [row('zero', [0, -0.0], [50000, 60000]),
                row('nonzero', [0], [50000]),
                row('nonzero', [-2, 2], [60000, 70000]),
                row('invalid', [None, 'NaN', '+Inf', '-Inf'], [50000, 60000, 70000, 80000]),
                row('missing', [4, 5], [39000, 101000]),
                row('tiny', [0.00001], [100000])]
        with patch.object(client, 'catalog', return_value=catalog), patch.object(client, '_export', return_value=iter(rows)):
            activity = client.recent_activity(lookback_seconds=60, end=100, zero_tolerance=0.001)
        by_name = {s.series.name: s for s in activity}
        self.assertEqual(by_name['zero'].status, 'zero_only')
        self.assertEqual(by_name['nonzero'].status, 'nonzero')
        self.assertEqual(by_name['nonzero'].nonzero_samples, 2)
        self.assertEqual(by_name['nonzero'].finite_samples, 3)
        self.assertEqual(by_name['nonzero'].last_sample_at, 70)
        self.assertEqual(by_name['invalid'].status, 'invalid_only')
        self.assertEqual(by_name['invalid'].invalid_samples, 4)
        self.assertEqual(by_name['missing'].status, 'no_data')
        self.assertEqual(by_name['tiny'].status, 'zero_only')

    def test_http_form_and_cluster_prefix(self):
        client = VictoriaMetricsClient('http://localhost:8481/select/0/prometheus')
        with patch('apps.metrics.urlopen', return_value=io.BytesIO(
            b'{"status":"success","data":["up","cpu","up"]}')) as transport:
            result = client.list_metric_names(['up', 'cpu'], lookback_seconds=60, end=100)
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, 'http://localhost:8481/select/0/prometheus/api/v1/label/__name__/values')
        self.assertEqual(parse_qs(request.data.decode()), {'match[]': ['up', 'cpu'], 'start': ['40'], 'end': ['100']})
        self.assertEqual(result, ['cpu', 'up'])

    def test_errors_are_not_empty_catalogs(self):
        client = VictoriaMetricsClient()
        for body in (b'not json', b'{"status":"error"}', b'{"status":"success","data":{}}'):
            with self.subTest(body=body), patch('apps.metrics.urlopen', return_value=io.BytesIO(body)):
                with self.assertRaises(VictoriaMetricsError):
                    client.catalog()
        with patch('apps.metrics.urlopen', side_effect=URLError('offline')):
            with self.assertRaises(VictoriaMetricsError):
                client.catalog()

    def test_export_chunks_and_malformed_rows(self):
        client = VictoriaMetricsClient()
        with patch('apps.metrics.urlopen', return_value=io.BytesIO(b'{}\n\n{}\n')):
            self.assertEqual(list(client._export([])), [{}, {}])
        with patch.object(client, 'catalog', return_value=MetricCatalog((), 0, 100)), patch.object(
            client, '_export', return_value=iter([{'metric': {'__name__': 'up'}, 'values': [1], 'timestamps': []}])):
            with self.assertRaises(VictoriaMetricsError):
                client.recent_activity(end=100)

    def test_empty_and_active_catalog(self):
        client = VictoriaMetricsClient()
        with patch.object(client, 'catalog', return_value=MetricCatalog((), 0, 100)), patch.object(
            client, '_export', return_value=iter([])):
            self.assertFalse(client.has_recent_nonzero(end=100))
        with patch.object(client, 'recent_activity', return_value=[]):
            active = client.active_catalog(end=100, lookback_seconds=60)
            self.assertEqual(active.series, ())
            self.assertEqual((active.start, active.end), (40, 100))

    def test_validation(self):
        for url in ('file:///tmp/data', 'http://localhost?x=1'):
            with self.assertRaises(ValueError):
                VictoriaMetricsClient(url)
        client = VictoriaMetricsClient()
        for kwargs in ({'lookback_seconds': 0}, {'end': float('nan')}, {'zero_tolerance': -1}):
            with self.assertRaises(ValueError):
                client.recent_activity(**kwargs)


if __name__ == '__main__':
    unittest.main()
