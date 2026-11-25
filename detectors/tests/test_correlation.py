"""Numeric references, cycle detection and point-window/result contracts."""

import io
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import pacf_burg

from apps.correlation import RollingCorrelationAnalyzer, fft_periods
from apps.generator import CycleConfig, NoiseConfig, SpikeConfig, TimeSeriesGenerator
from apps.generator.logging import configure_json_logging, logger
from apps.metrics import DataframeResult, MetricResult


def series(points=200, cycles=(), seed=41, **kwargs):
    return TimeSeriesGenerator(mean=100, noise=NoiseConfig(1, 0, 0, 0),
                              spikes=SpikeConfig(probability=0), cycles=cycles,
                              seed=seed, start_time=0, **kwargs).generate(points)


class PeriodTests(unittest.TestCase):
    def test_six_base_scenarios_detect_cycles_and_resolve_windows(self):
        from tests.visualize_correlation import scenarios
        for slug, _, metric, expected in scenarios():
            with self.subTest(case=slug):
                result = RollingCorrelationAnalyzer(window=20).analyze(metric)
                self.assertEqual(result.values.attrs['periods'], expected)
                self.assertEqual(result.window, 25 if len(expected) == 2 else 20)
                self.assertEqual(result.values.attrs['max_lag'], 7 if len(expected) == 2 else 5)
                self.assertEqual(result.values.attrs['step'], 12 if len(expected) == 2 else 10)
                self.assertTrue(np.isfinite(result.values.to_numpy()).all())

    def test_refines_non_fft_bin_period_365(self):
        metric = series(1000, cycles=(CycleConfig(365, 10),))
        result = RollingCorrelationAnalyzer(window=50).analyze(metric)
        self.assertEqual(result.values.attrs['periods'], [365])
        self.assertEqual(result.window, 91)
        self.assertEqual(result.values.attrs['step'], 45)
        self.assertEqual(result.values.attrs['max_lag'], 23)

    def test_short_period_10_keeps_requested_window_50(self):
        result = RollingCorrelationAnalyzer(50).analyze(series(cycles=(CycleConfig(10, 5),)))
        self.assertEqual(result.values.attrs['periods'], [10])
        self.assertEqual(result.window, 50)
        self.assertEqual(result.values.attrs['max_lag'], 13)

    def test_cycle_function_is_selected_and_called_once_on_copy(self):
        metric = series()
        original = list(metric.values)
        calls = []
        def selected(values):
            calls.append(len(values))
            values[:] = 0
            return (10, 100, 100)
        result = RollingCorrelationAnalyzer(20, cycle_function=selected).analyze(metric)
        self.assertEqual(calls, [200])
        self.assertEqual(metric.values, original)
        self.assertEqual(result.values.attrs['periods'], [10, 100])
        self.assertEqual(result.window, 25)
        self.assertTrue(np.isfinite(result.values.to_numpy()).all())

    def test_no_false_periods_on_white_noise_seeds_and_constant(self):
        for seed in (1, 7, 41, 99):
            with self.subTest(seed=seed):
                self.assertEqual(fft_periods(np.asarray(series(seed=seed).values)), ())
        self.assertEqual(fft_periods(np.ones(200)), ())
        self.assertEqual(fft_periods(np.ones(3)), ())

    def test_cycle_function_and_fft_parameter_validation(self):
        for detected in ((1,), (-1,), (10.5,), (True,)):
            with self.subTest(periods=detected), self.assertRaises(ValueError):
                RollingCorrelationAnalyzer(20, cycle_function=lambda values: detected).analyze(series())
        for kwargs in ({'min_period': 1}, {'min_cycles': 1},
                       {'min_power_fraction': 0}, {'max_cycles': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                fft_periods(np.ones(200), **kwargs)
        with self.assertRaises(ValueError):
            fft_periods(np.asarray([1, np.nan, 2]))


class RollingTests(unittest.TestCase):
    def test_1000_points_window_50_matches_requested_intervals_and_lag_13(self):
        metric = series(1000)
        result = RollingCorrelationAnalyzer(50, cycle_function=lambda values: ()).analyze(metric)
        self.assertIsInstance(result, DataframeResult)
        self.assertEqual(result.window, 50)
        self.assertEqual(result.timestamps, [i * 15 for i in range(0, 951, 25)])
        self.assertEqual(len(result.values), 39)
        self.assertEqual(result.values.index.tolist(), result.timestamps)
        self.assertEqual(result.values.columns.names, ['function', 'lag'])
        self.assertEqual(result.values['acf'].columns.tolist(), list(range(14)))
        self.assertEqual(result.values['pacf'].columns.tolist(), list(range(14)))
        self.assertEqual(result.timestamps[-1] // 15 + result.window, 1000)

    def test_200_points_window_20_has_19_windows_lag_5(self):
        result = RollingCorrelationAnalyzer(20).analyze(series())
        self.assertEqual(result.timestamps, [i * 15 for i in range(0, 181, 10)])
        self.assertEqual(result.values.shape, (19, 12))
        self.assertEqual(result.values.attrs['max_lag'], 5)
        np.testing.assert_allclose(result.values[('acf', 0)], 1)
        np.testing.assert_allclose(result.values[('pacf', 0)], 1)

    def test_acf_matches_direct_definition_every_window(self):
        metric = series()
        result = RollingCorrelationAnalyzer(20).analyze(metric)
        for row, stamp in enumerate(result.timestamps):
            start = stamp // 15
            values = np.asarray(metric.values[start:start + 20])
            values -= values.mean()
            energy = values @ values
            expected = [1] + [float(values[:-lag] @ values[lag:]) / energy for lag in range(1, 6)]
            np.testing.assert_allclose(result.values['acf'].iloc[row], expected, atol=1e-12)

    def test_pacf_matches_known_lag_one_and_statsmodels_on_every_window(self):
        metric = series()
        result = RollingCorrelationAnalyzer(20).analyze(metric)
        for row, stamp in enumerate(result.timestamps):
            start = stamp // 15
            values = np.asarray(metric.values[start:start + 20])
            values -= values.mean()
            lag_one = 2 * (values[:-1] @ values[1:]) / (values[:-1] @ values[:-1] + values[1:] @ values[1:])
            self.assertAlmostEqual(result.values[('pacf', 1)].iloc[row], lag_one)
            expected, _ = pacf_burg(values, nlags=5, demean=False)
            np.testing.assert_allclose(result.values['pacf'].iloc[row], expected, atol=1e-12)

    def test_explicit_lag_expands_window_and_has_precedence(self):
        metric = series()
        analyzer = RollingCorrelationAnalyzer(20, max_lag=8, cycle_function=lambda values: (100,))
        result = analyzer.analyze(metric)
        self.assertEqual(result.window, 32)
        self.assertEqual(result.values.attrs['step'], 16)
        self.assertEqual(result.values.attrs['max_lag'], 8)
        self.assertEqual(analyzer.window, 20)
        # A longer cycle can further expand the window without changing explicit lag.
        result = RollingCorrelationAnalyzer(20, max_lag=8, cycle_function=lambda values: (400,)).analyze(metric)
        self.assertEqual(result.window, 100)
        self.assertEqual(result.values.attrs['max_lag'], 8)

    def test_odd_window_and_incomplete_tail(self):
        result = RollingCorrelationAnalyzer(25, cycle_function=lambda values: ()).analyze(series())
        self.assertEqual(result.timestamps, [i * 15 for i in range(0, 176, 12)])
        self.assertEqual(result.timestamps[-1] // 15 + result.window, 193)
        self.assertEqual(result.values.attrs['max_lag'], 7)

    def test_exactly_one_window_short_or_empty_input(self):
        for length, expected_rows in ((20, 1), (19, 0), (0, 0)):
            with self.subTest(length=length):
                metric = series(length) if length else MetricResult(name='empty')
                result = RollingCorrelationAnalyzer(20, cycle_function=lambda values: ()).analyze(metric)
                self.assertEqual(result.values.shape, (expected_rows, 12))
                self.assertEqual(len(result.timestamps), expected_rows)

    def test_constant_windows_are_undefined_not_spurious_zero_correlations(self):
        metric = MetricResult(name='flat', values=[100] * 200, timestamps=list(range(200)))
        result = RollingCorrelationAnalyzer(20).analyze(metric)
        self.assertTrue(result.values.isna().all().all())
        self.assertEqual(result.values.attrs['constant_windows'], 19)

    def test_result_preserves_metadata_input_and_independence(self):
        metric = series(tags={'host': 'original'})
        values, timestamps = list(metric.values), list(metric.timestamps)
        result = RollingCorrelationAnalyzer(20).analyze(metric)
        self.assertEqual(result.name, metric.name)
        self.assertEqual(result.tags, metric.tags)
        result.tags['host'] = 'changed'
        result.timestamps[0] = -1
        self.assertEqual(metric.tags, {'host': 'original'})
        self.assertEqual(metric.values, values)
        self.assertEqual(metric.timestamps, timestamps)

    def test_uniform_timestamps_required_and_validation(self):
        metric = series()
        metric.timestamps[-1] += 1
        with self.assertRaisesRegex(ValueError, 'uniformly spaced'):
            RollingCorrelationAnalyzer(20).analyze(metric)
        for kwargs in ({'window': 3}, {'window': 20.5}, {'max_lag': 0}, {'max_lag': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RollingCorrelationAnalyzer(**kwargs)
        with self.assertRaises(TypeError):
            RollingCorrelationAnalyzer(cycle_function=None)


class ResultTests(unittest.TestCase):
    def test_dataframe_result_copies_frame_and_stores_window_start_timestamps(self):
        frame = pd.DataFrame({'feature': [0.5, 0.8]})
        stamps = [100, 115]
        result = DataframeResult('metric', {'host': 'test'}, frame, stamps, 20)
        frame.iloc[0, 0] = 99
        stamps[0] = -1
        self.assertEqual(result.values.iloc[0, 0], 0.5)
        self.assertEqual(result.values.index.tolist(), [100, 115])
        self.assertEqual(result.timestamps, [100, 115])
        self.assertEqual(result.window, 20)

    def test_dataframe_result_validation(self):
        with self.assertRaises(TypeError):
            DataframeResult('metric', {}, [1], [0], 20)
        with self.assertRaises(ValueError):
            DataframeResult('metric', {}, pd.DataFrame({'x': [1]}), [], 20)
        with self.assertRaises(ValueError):
            DataframeResult('metric', {}, pd.DataFrame(), [], 0)

    def test_json_logging_includes_effective_window_and_periods(self):
        handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
        stream = io.StringIO()
        try:
            configure_json_logging(stream=stream)
            RollingCorrelationAnalyzer(20, cycle_function=lambda values: (100,)).analyze(series())
            row = json.loads(stream.getvalue().splitlines()[-1])
            self.assertEqual(row['event'], 'correlations_calculated')
            self.assertEqual(row['details']['window'], 25)
            self.assertEqual(row['details']['max_lag'], 7)
            self.assertEqual(row['details']['periods'], [100])
        finally:
            for handler in logger.handlers:
                if handler not in handlers:
                    handler.close()
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate

    def test_visualization_and_csv_exports_for_all_eight_series(self):
        from tests.visualize_correlation import create_report
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            create_report(output)
            self.assertEqual((output / 'report.png').read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual((output / 'anomalies.png').read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
            rows = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(len(rows), 8)
            self.assertEqual(sum(row['group'] == 'baseline' for row in rows), 6)
            self.assertEqual(sum(row['group'] == 'anomaly' for row in rows), 2)
            for row in rows:
                self.assertEqual(row['points'], 200)
                self.assertEqual(row['input_window'], 20)
                if row['group'] == 'baseline':
                    self.assertEqual(row['detected_periods'], row['expected_periods'])
                frame = pd.read_csv(output / f'{row["scenario"]}-correlations.csv', header=[0, 1], index_col=0)
                self.assertEqual(len(frame), row['windows'])
                self.assertEqual(len(frame.columns), 2 * (row['max_lag'] + 1))
                samples = pd.read_csv(output / f'{row["scenario"]}-series.csv')
                self.assertEqual(len(samples), 200)
                if row['group'] == 'anomaly':
                    self.assertEqual(row['noise_variance_multiplier'],
                                     2.0 if row['anomaly_kind'] == 'trend' else 1.0)
                    original = pd.read_csv(output / f'{row["scenario"]}-original.csv')
                    self.assertEqual(len(original), 200)
                    start, stop = row['anomaly_range']
                    np.testing.assert_array_equal(samples['value'][:start], original['value'][:start])
                    np.testing.assert_array_equal(samples['value'][stop:], original['value'][stop:])


class ScenarioTests(unittest.TestCase):
    def test_high_noise_variants_have_large_noise_and_keep_configured_cycles(self):
        from tests.visualize_correlation import scenarios
        cases = {slug: (metric, periods) for slug, _, metric, periods in scenarios()}
        self.assertEqual(len(cases), 6)
        for normal_slug, noisy_slug in (('mixed-noise', 'high-noise'),
                                        ('short-cycle', 'high-noise-cycle')):
            normal, periods = cases[normal_slug]
            noisy, noisy_periods = cases[noisy_slug]
            self.assertEqual(periods, noisy_periods)
            wave = 5 * np.sin(2 * np.pi * np.arange(200) / 10) if periods else 0
            normal_noise = np.asarray(normal.values) - 100 - wave
            noisy_noise = np.asarray(noisy.values) - 100 - wave
            self.assertGreater(noisy_noise.std(), normal_noise.std() * 5)
            self.assertEqual(len(noisy.values), 200)

    def test_trend_starts_at_midpoint_with_exact_ten_degree_ramp(self):
        from tests.visualize_correlation import anomaly_scenarios
        _, _, original, metric, metadata = next(anomaly_scenarios())
        self.assertEqual(metadata['anomaly_range'], [100, 200])
        self.assertEqual(metadata['angle_axes'], 'point_index, metric_value')
        self.assertEqual(metric.values[:100], original.values[:100])
        offsets = np.asarray(metric.values[100:]) - original.values[100:]
        slope = np.tan(np.deg2rad(10))
        np.testing.assert_allclose(offsets, np.arange(100) * slope, atol=1e-12)
        self.assertAlmostEqual(np.rad2deg(np.arctan(offsets[-1] / 99)), 10)
        self.assertEqual(metric.timestamps, original.timestamps)
        self.assertEqual(len(metric.values), 200)

    def test_burst_adds_twenty_percent_to_exactly_fifteen_points_and_recovers_original(self):
        from tests.visualize_correlation import anomaly_scenarios
        _, _, original, metric, metadata = list(anomaly_scenarios())[1]
        self.assertEqual(metadata['anomaly_range'], [100, 115])
        self.assertEqual(metadata['multiplier'], 1.2)
        self.assertEqual(metadata['duration_points'], 15)
        self.assertEqual(metric.values[:100], original.values[:100])
        np.testing.assert_allclose(metric.values[100:115], np.asarray(original.values[100:115]) * 1.2)
        self.assertEqual(metric.values[115:], original.values[115:])
        self.assertEqual(np.count_nonzero(np.asarray(metric.values) != original.values), 15)
        self.assertEqual(metric.timestamps, original.timestamps)
        self.assertEqual(len(metric.values), 200)

    def test_trend_noise_variance_doubles_after_removing_unchanged_ramp(self):
        from tests.visualize_correlation import anomaly_scenarios
        cases = list(anomaly_scenarios())
        _, _, trend_original, trend_metric, metadata = cases[0]
        _, _, control, _, _ = cases[1]
        ramp = np.maximum(np.arange(200) - 100, 0) * np.tan(np.deg2rad(10))
        detrended = np.asarray(trend_metric.values) - ramp
        np.testing.assert_allclose(detrended, trend_original.values, atol=1e-12)
        # The same seed provides a paired comparison of realized noise variance.
        self.assertAlmostEqual(np.var(detrended) / np.var(control.values), 2.0, places=12)
        self.assertEqual(metadata['noise_variance_multiplier'], 2.0)
        self.assertEqual(metadata['angle_degrees'], 10.0)

    def test_anomaly_fixtures_repeat_and_rolling_features_are_finite(self):
        from tests.visualize_correlation import anomaly_scenarios
        first, second = list(anomaly_scenarios()), list(anomaly_scenarios())
        for (_, _, original, metric, _), (_, _, repeated_original, repeated, _) in zip(first, second):
            self.assertEqual(original.values, repeated_original.values)
            self.assertEqual(metric.values, repeated.values)
            result = RollingCorrelationAnalyzer(20).analyze(metric)
            self.assertTrue(np.isfinite(result.values.to_numpy()).all())
            self.assertEqual(result.timestamps[0], metric.timestamps[0])


if __name__ == '__main__':
    unittest.main()
