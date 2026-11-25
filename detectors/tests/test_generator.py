"""Contract, distribution, boundary and visualization checks for the emulator."""

from datetime import datetime, timedelta, timezone
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from apps.generator import (
    AnomalyGenerator, CycleConfig, MetricResult, NoiseConfig, SpikeConfig,
    TimeSeriesGenerator, configure_json_logging,
)
from apps.generator.logging import logger
from apps.runtime_trend import RuntimeTrendDetector, TrendConfig


QUIET = NoiseConfig(0, 0, 0, 0)
NO_SPIKES = SpikeConfig(probability=0)


def flat(points=20, **kwargs):
    return TimeSeriesGenerator(mean=100, noise=QUIET, spikes=NO_SPIKES,
                               start_time=0, **kwargs).generate(points)


class MetricResultTests(unittest.TestCase):
    def test_attached_constructor_and_independent_containers(self):
        tags, values, stamps = {'host': 'test'}, [1, 2.5], [0, 15]
        metric = MetricResult(None, 'rate', tags, values, stamps)
        tags['host'] = 'changed'
        values[0] = 99
        stamps[0] = -1
        self.assertEqual(metric.tags, {'host': 'test'})
        self.assertEqual(metric.values, [1, 2.5])
        self.assertEqual(metric.timestamps, [0, 15])

    def test_prometheus_matrix_json(self):
        payload = {'metric': {'__name__': 'rate', 'host': 'test'},
                   'values': [[0, '1.5'], [15, '2']]}
        metric = MetricResult(payload, 'explicit_name', None)
        self.assertEqual(metric.name, 'explicit_name')
        self.assertEqual(metric.tags, {'host': 'test'})
        self.assertEqual(metric.values, [1.5, 2.0])
        self.assertEqual(metric.timestamps, [0, 15])
        self.assertEqual(MetricResult(payload).name, 'rate')

    def test_invalid_metric_data(self):
        for kwargs in (
            {'values': [1], 'timestamps': []},
            {'values': [float('nan')], 'timestamps': [0]},
            {'values': [float('inf')], 'timestamps': [0]},
            {'values': [True], 'timestamps': [0]},
            {'values': [1], 'timestamps': [0.5]},
            {'values': [1], 'timestamps': [False]},
            {'values': [1, 2], 'timestamps': [0, 0]},
            {'values': [1, 2], 'timestamps': [15, 0]},
            {'tags': {'host': 1}},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MetricResult(name='test', **kwargs)
        with self.assertRaises(ValueError):
            MetricResult({'metric': {'__name__': 'test'}, 'values': [[0.5, '1']]})


class TimeSeriesTests(unittest.TestCase):
    def test_disabled_noise_spikes_and_cycles_produce_constant(self):
        metric = flat(256)
        self.assertEqual(metric.values, [100.0] * 256)
        self.assertEqual(metric.timestamps, list(range(0, 256 * 15, 15)))
        self.assertIsInstance(metric, MetricResult)

    def test_current_day_midnight_and_default_step(self):
        class Clock(datetime):
            @classmethod
            def now(cls):
                return cls(2026, 10, 4, 17, 41, 32, 123456)

        with patch('apps.generator.series.datetime', Clock):
            generator = TimeSeriesGenerator()
        expected = int(datetime(2026, 10, 4).timestamp())
        self.assertEqual(generator.generate(3).timestamps,
                         [expected, expected + 15, expected + 30])
        # Time remains fixed after construction, even when the current day changes.
        self.assertEqual(generator.generate(1).timestamps, [expected])

    def test_custom_time_timezone_and_interval_cross_midnight(self):
        start = datetime(2026, 10, 4, 23, 59, 55, tzinfo=timezone(timedelta(hours=3)))
        metric = TimeSeriesGenerator(start_time=start, step_seconds=7).generate(3)
        stamp = int(start.timestamp())
        self.assertEqual(metric.timestamps, [stamp, stamp + 7, stamp + 14])
        self.assertEqual(TimeSeriesGenerator(start_time=-30, step_seconds=1)
                         .generate(1).timestamps, [-30])

    def test_seed_reproducibility_and_instance_rng(self):
        a = TimeSeriesGenerator(seed=42, start_time=0)
        b = TimeSeriesGenerator(seed=42, start_time=0)
        self.assertEqual(a.generate(960).values, b.generate(960).values)
        first = a.generate(960).values
        second = a.generate(960).values
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, TimeSeriesGenerator(seed=43, start_time=0).generate(960).values)
        state = np.random.get_state()
        TimeSeriesGenerator(seed=5).generate(100)
        after = np.random.get_state()
        np.testing.assert_array_equal(state[1], after[1])
        self.assertEqual(state[2:], after[2:])

    def test_default_noise_stationary_near_configured_mean_across_seeds(self):
        for seed in (1, 7, 42, 99):
            with self.subTest(seed=seed):
                metric = TimeSeriesGenerator(mean=4300, seed=seed, spikes=NO_SPIKES,
                                             start_time=0).generate(20000)
                values = np.asarray(metric.values)
                self.assertTrue(np.isfinite(values).all())
                self.assertLess(abs(values.mean() - 4300), 4300 * 0.002)
                slope = np.polyfit(np.arange(len(values)), values, 1)[0]
                self.assertLess(abs(slope) * len(values), 4300 * 0.002)
                self.assertGreater(values.std(), 4300 * 0.003)
                self.assertLess(values.std(), 4300 * 0.008)

    def test_noise_components_have_configured_scale_and_shape(self):
        components = (
            (NoiseConfig(3, 0, 0, 0), 30),
            (NoiseConfig(0, 2, 0.8, 0), 20),
            (NoiseConfig(0, 0, 0, 1.5, 5), 15),
        )
        for cfg, expected_std in components:
            with self.subTest(config=cfg):
                values = np.asarray(TimeSeriesGenerator(mean=1000, noise=cfg,
                                    spikes=NO_SPIKES, seed=123, start_time=0)
                                    .generate(40000).values) - 1000
                self.assertLess(abs(values.std() - expected_std), expected_std * 0.1)
                if cfg.correlated_std_percent:
                    self.assertAlmostEqual(np.corrcoef(values[:-1], values[1:])[0, 1],
                                           0.8, delta=0.03)
                if cfg.heavy_tail_std_percent:
                    kurtosis = np.mean((values - values.mean()) ** 4) / values.var() ** 2
                    self.assertGreater(kurtosis, 4)

    def test_three_cycles_use_points_and_percent_peak_amplitude(self):
        cycles = (CycleConfig(16, 10), CycleConfig(32, 3), CycleConfig(8, 2, 0.3))
        values = TimeSeriesGenerator(mean=200, cycles=cycles, noise=QUIET,
                                     spikes=NO_SPIKES, start_time=0).generate(128).values
        indices = np.arange(128)
        expected = 200 + sum(2 * c.amplitude_percent * np.sin(
            2 * np.pi * indices / c.period_points + c.phase_radians) for c in cycles)
        np.testing.assert_allclose(values, expected, atol=1e-12)

    def test_negative_and_zero_baselines(self):
        negative = TimeSeriesGenerator(mean=-200, noise=QUIET, spikes=NO_SPIKES,
                    cycles=(CycleConfig(4, 10),), start_time=0).generate(4)
        np.testing.assert_allclose(negative.values, [-200, -180, -200, -220])
        self.assertEqual(TimeSeriesGenerator(mean=0, seed=42).generate(10).values, [0] * 10)

    def test_rare_default_background_spikes(self):
        generator = TimeSeriesGenerator(mean=100, noise=QUIET, seed=42, start_time=0)
        metric = generator.generate(20000)
        ranges = generator.background_anomaly_ranges
        self.assertGreater(len(ranges), 0)
        self.assertLess(len(ranges), 60)
        affected = np.zeros(20000, dtype=bool)
        for start, stop in ranges:
            self.assertIn(stop - start, (1, 2, 3))
            affected[start:stop] = True
        self.assertLess(affected.mean(), 0.01)
        values = np.asarray(metric.values)
        self.assertTrue(np.all((values[affected] >= 101) & (values[affected] <= 108)))
        self.assertTrue(np.all(values[~affected] == 100))

    def test_spikes_have_maximum_three_points_fifty_percent_and_gaps(self):
        generator = TimeSeriesGenerator(mean=100, noise=QUIET, start_time=0, seed=5,
            spikes=SpikeConfig(probability=1, min_amplitude_percent=50,
                               max_amplitude_percent=50, max_points=3))
        metric = generator.generate(50)
        ranges = generator.background_anomaly_ranges
        self.assertTrue(all(1 <= stop - start <= 3 for start, stop in ranges))
        self.assertTrue(all(b < c for (_, b), (c, _) in zip(ranges, ranges[1:])))
        self.assertEqual(set(metric.values), {100, 150})
        self.assertEqual(metric.values[0], 150)

    def test_constructor_and_count_validation(self):
        for kwargs in (
            {'mean': float('nan')}, {'mean': float('inf')}, {'mean': True},
            {'step_seconds': 0}, {'step_seconds': 0.5}, {'step_seconds': True},
            {'start_time': 1.2}, {'start_time': False},
            {'start_time': datetime(2026, 10, 4, microsecond=1)},
            {'seed': -1}, {'seed': 1.5}, {'seed': True},
            {'cycles': (CycleConfig(4, 1),) * 4}, {'cycles': (4,)},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TimeSeriesGenerator(**kwargs)
        for count in (0, -1, 1.5, True):
            with self.subTest(points=count), self.assertRaises(ValueError):
                TimeSeriesGenerator().generate(count)

    def test_config_validation(self):
        for factory, kwargs in (
            (NoiseConfig, {'white_std_percent': -1}),
            (NoiseConfig, {'correlation': 1}),
            (NoiseConfig, {'correlation': -1}),
            (NoiseConfig, {'heavy_tail_std_percent': float('nan')}),
            (NoiseConfig, {'degrees_of_freedom': 2}),
            (CycleConfig, {'period_points': 1, 'amplitude_percent': 1}),
            (CycleConfig, {'period_points': 4, 'amplitude_percent': -1}),
            (CycleConfig, {'period_points': 4, 'amplitude_percent': 1, 'phase_radians': float('inf')}),
            (SpikeConfig, {'probability': -0.1}),
            (SpikeConfig, {'probability': 1.1}),
            (SpikeConfig, {'max_points': 4}),
            (SpikeConfig, {'max_points': 0}),
            (SpikeConfig, {'max_amplitude_percent': 51}),
            (SpikeConfig, {'min_amplitude_percent': 10, 'max_amplitude_percent': 8}),
        ):
            with self.subTest(factory=factory, kwargs=kwargs), self.assertRaises(ValueError):
                factory(**kwargs)


class AnomalyTests(unittest.TestCase):
    def test_exact_index_range_uses_original_values_and_preserves_metadata(self):
        original = MetricResult(None, 'rate', {'host': 'test'}, list(range(10)),
                                list(range(0, 150, 15)))
        seen = []
        def anomaly(value):
            seen.append(value)
            return value * 2 + 10
        modified = AnomalyGenerator(original, 3, 6, anomaly).generate()
        self.assertEqual(seen, [3, 4, 5])
        self.assertEqual(modified.values, [0, 1, 2, 16, 18, 20, 6, 7, 8, 9])
        self.assertEqual(original.values, list(range(10)))
        self.assertEqual(modified.timestamps, original.timestamps)
        self.assertEqual((modified.name, modified.tags), (original.name, original.tags))
        self.assertIsNot(modified.values, original.values)
        self.assertIsNot(modified.timestamps, original.timestamps)
        self.assertIsNot(modified.tags, original.tags)

    def test_snapshot_and_repeated_generation(self):
        original = flat(10, tags={'host': 'test'})
        generator = AnomalyGenerator(original, 0, 1, lambda value: value * 2)
        original.values[0] = 300
        original.tags['host'] = 'changed'
        first = generator.generate()
        first.values[0] = 500
        first.timestamps[0] = -1
        first.tags['host'] = 'result_changed'
        second = generator.generate()
        self.assertEqual(second.values, [200] + [100] * 9)
        self.assertEqual(second.tags, {'host': 'test'})
        self.assertEqual(second.timestamps[0], 0)

    def test_last_point_whole_series_and_multiple_ranges(self):
        original = flat(5)
        modified = AnomalyGenerator(original, 4, 5, lambda value: 0).generate()
        self.assertEqual(modified.values, [100, 100, 100, 100, 0])
        modified = AnomalyGenerator(modified, 0, 2, lambda value: value * 1.5).generate()
        self.assertEqual(modified.values, [150, 150, 100, 100, 0])
        modified = AnomalyGenerator(original, 0, 5, lambda value: -value).generate()
        self.assertEqual(modified.values, [-100] * 5)

    def test_invalid_ranges_and_callback(self):
        for start, stop in ((-1, 2), (0, 0), (3, 2), (0, 11), (True, 2), (1.5, 2)):
            with self.subTest(start=start, stop=stop), self.assertRaises(ValueError):
                AnomalyGenerator(flat(10), start, stop, lambda value: value)
        with self.assertRaises(TypeError):
            AnomalyGenerator(flat(), 0, 1, None)

    def test_invalid_results_or_late_callback_error_never_modify_original(self):
        original = flat(5)
        for bad in (float('nan'), float('inf'), '100', True, None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                AnomalyGenerator(original, 1, 3, lambda value: bad).generate()
            self.assertEqual(original.values, [100] * 5)
        calls = []
        def failing(value):
            calls.append(value)
            if len(calls) == 2:
                raise RuntimeError('callback error')
            return value * 2
        with self.assertRaisesRegex(RuntimeError, 'callback error'):
            AnomalyGenerator(original, 0, 3, failing).generate()
        self.assertEqual(original.values, [100] * 5)

    def test_original_and_anomalous_series_replay_in_existing_detector(self):
        original = flat(80)
        modified = AnomalyGenerator(original, 20, 23, lambda value: value * 1.5).generate()
        for metric in (original, modified):
            detector = RuntimeTrendDetector(TrendConfig(120, 1, 1))
            for timestamp, value in zip(metric.timestamps, metric.values):
                detector.update(timestamp, value)
            self.assertTrue(detector.evaluate().ready)
            self.assertFalse(detector.evaluate().detected)


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.handlers, self.level, self.propagate = list(logger.handlers), logger.level, logger.propagate

    def tearDown(self):
        for handler in logger.handlers:
            if handler not in self.handlers:
                handler.close()
        logger.handlers = self.handlers
        logger.setLevel(self.level)
        logger.propagate = self.propagate

    def test_json_events_metadata_and_configuration_idempotence(self):
        stream = io.StringIO()
        root_handlers = list(logging.getLogger().handlers)
        configure_json_logging(stream=stream)
        configure_json_logging(stream=stream)
        original = TimeSeriesGenerator(mean=4300, start_time=0, seed=42).generate(50)
        AnomalyGenerator(original, 3, 6, lambda value: value * 1.5).generate()
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([row['event'] for row in rows], ['series_generated', 'anomaly_applied'])
        self.assertEqual(rows[0]['details']['mean'], 4300)
        self.assertEqual(rows[0]['details']['step_seconds'], 15)
        self.assertEqual(rows[1]['details']['affected_points'], 3)
        self.assertEqual(rows[0]['level'], 'INFO')
        self.assertIn('timestamp', rows[0])
        self.assertEqual(logging.getLogger().handlers, root_handlers)

    def test_callback_failure_is_json_with_traceback(self):
        stream = io.StringIO()
        configure_json_logging(stream=stream)
        original = flat(3)
        with self.assertRaises(ZeroDivisionError):
            AnomalyGenerator(original, 0, 1, lambda value: value / 0).generate()
        row = json.loads(stream.getvalue().splitlines()[-1])
        self.assertEqual(row['event'], 'anomaly_failed')
        self.assertEqual(row['details']['point_index'], 0)
        self.assertIn('ZeroDivisionError', row['exception'])

    def test_numpy_numeric_parameters_remain_json_serializable(self):
        stream = io.StringIO()
        configure_json_logging(stream=stream)
        metric = TimeSeriesGenerator(
            mean=np.float64(100), start_time=np.int64(0), step_seconds=np.int64(15),
            seed=np.int64(42), cycles=(CycleConfig(np.int64(8), np.float64(1)),),
            noise=NoiseConfig(white_std_percent=np.float64(0.5)),
            spikes=SpikeConfig(max_points=np.int64(3)),
        ).generate(np.int64(10))
        self.assertEqual(json.loads(stream.getvalue())['event'], 'series_generated')
        converted = MetricResult(name='numpy', values=np.asarray([1, 2]),
                                 timestamps=np.asarray([0, 15]))
        self.assertEqual(json.loads(json.dumps(converted.values)), [1, 2])
        self.assertEqual(len(metric.values), 10)


class VisualizationTests(unittest.TestCase):
    def test_headless_report_exports_original_and_anomalous_metrics(self):
        from tests.visualize_generator import create_report
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            create_report(output)
            self.assertEqual((output / 'report.png').read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
            payload = json.loads((output / 'series.json').read_text(encoding='utf-8'))
            self.assertEqual(len(payload['scenarios']), 5)
            for case in payload['scenarios']:
                for key in ('original', 'anomalous'):
                    metric = MetricResult(**case[key])
                    self.assertEqual(len(metric.values), 960)
                if case['anomaly_range']:
                    start, stop = case['anomaly_range']
                    self.assertEqual(case['original']['values'][:start], case['anomalous']['values'][:start])
                    self.assertEqual(case['original']['values'][stop:], case['anomalous']['values'][stop:])


if __name__ == '__main__':
    unittest.main()
