"""A healthy cycle is learned before scoring; future structure remains visible."""

from dataclasses import replace
import unittest

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from tests.acf_service_scenarios import load_snapshots
from tests.mad_cycle_scenarios import (MAD_CONFIG, TRAIN_SAMPLES, describe_comparison,
                                      fit_healthy_cycle, replay_without_cycle)


class DeseasonalizedMadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases, _ = load_snapshots()
        cls.results = {c.name: replay_without_cycle(c) for c in cls.cases}

    def test_fractional_period_is_recovered_and_heldout_cycle_removed(self):
        rng = np.random.default_rng(43)
        positions = np.arange(1200)
        phase = 2*np.pi*positions/91.7
        values = 40+2*np.sin(phase)+.6*np.cos(2*phase)+rng.normal(0, .03, 1200)
        model = fit_healthy_cycle(values[:720])
        self.assertAlmostEqual(model.period_samples, 91.7, delta=.15)
        clean = model.residual(values[720:], start=720)
        self.assertLess(float(np.std(clean)), .12)
        self.assertGreater(model.training_r2, .95)

    def test_fixed_cycle_preserves_later_level_and_amplitude_changes(self):
        index = np.arange(720)
        healthy = 40+2*np.sin(2*np.pi*index/92)
        model = fit_healthy_cycle(healthy)
        future = np.arange(720, 1200)
        normal = model.predict(future)
        shifted = model.residual(normal+5, start=720)
        np.testing.assert_allclose(shifted, 5, atol=1e-12)
        larger = model.baseline_ms+3*model.cycle_at(future)
        np.testing.assert_allclose(model.residual(larger, start=720), 2*model.cycle_at(future), atol=1e-12)

    def test_real_inputs_are_preserved_and_heldout_cycle_energy_is_reduced(self):
        for case in self.cases:
            with self.subTest(case=case.name):
                rows, model = self.results[case.name]
                np.testing.assert_array_equal([r['raw_value_ms'] for r in rows], case.values)
                reconstructed = np.array([r['residual_ms']+r['healthy_prediction_ms'] for r in rows])
                np.testing.assert_allclose(reconstructed, case.values, atol=1e-12)
                self.assertTrue(all(r['phase'] == 'cycle_training' for r in rows[:720]))
                self.assertTrue(all(not r['ready'] for r in rows[:819]))
                self.assertEqual(rows[819]['timestamp_msk'], '2026-10-05T03:24:45+03:00')
                self.assertTrue(all(r['ready'] for r in rows[819:]))
                self.assertTrue(20 < model.period_samples*.25 < 25)
                heldout = describe_comparison(rows, model)['segments'][0]
                self.assertLess(heldout['residual_std_ms'], .65*heldout['raw_std_ms'])

    def test_both_mad_streams_match_independent_numpy_after_training(self):
        for case in self.cases:
            rows, _ = self.results[case.name]
            for name, column in (('raw', 'raw_value_ms'), ('residual', 'residual_ms')):
                with self.subTest(case=case.name, stream=name):
                    values = np.asarray([r[column] for r in rows[TRAIN_SAMPLES:]])
                    windows = sliding_window_view(values, MAD_CONFIG['data_max_size'])
                    local = windows[:, -MAD_CONFIG['window']:]
                    global_mad = np.mean(np.abs(windows-windows.mean(axis=1, keepdims=True)), axis=1)
                    local_mad = np.mean(np.abs(local-local.mean(axis=1, keepdims=True)), axis=1)
                    expected = np.tanh(.5*np.abs(local_mad-global_mad)/global_mad)
                    actual = rows[TRAIN_SAMPLES+MAD_CONFIG['data_max_size']-1:]
                    np.testing.assert_allclose([r[name+'_score'] for r in actual], expected, atol=1e-11)
                    np.testing.assert_allclose([r[name+'_reference_mad_ms'] for r in actual], global_mad, atol=1e-11)

    def test_later_data_do_not_change_template_or_past_scores(self):
        case = self.cases[0]
        prefix = replace(case, values=case.values[:1400], timestamps=case.timestamps[:1400])
        rows, model = replay_without_cycle(prefix)
        full_rows, full_model = self.results[case.name]
        self.assertEqual(model, full_model)
        self.assertEqual(rows, full_rows[:1400])

    def test_invalid_training_and_absent_cycle_are_rejected(self):
        for data in (np.ones(720), np.arange(30), [0]*719+[np.nan], [[1, 2], [3, 4]]):
            with self.subTest(shape=np.shape(data)), self.assertRaises(ValueError):
                fit_healthy_cycle(data)
        with self.assertRaises(ValueError):
            fit_healthy_cycle(np.sin(np.arange(720)), phase_bins=2)


if __name__ == '__main__':
    unittest.main()
