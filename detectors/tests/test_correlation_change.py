"""Established cross-series relationship, causal streaming and numerical contracts."""
import unittest

import numpy as np
from scipy.stats import pearsonr

from apps.detectors import CorrelationDetector as OnlineCorrelationDetector


def CorrelationDetector(*args, **kwargs):
    """Regression coverage of the optional explicit frozen-reference mode."""
    kwargs.setdefault('alert_threshold', .2)
    return OnlineCorrelationDetector(*args, adaptive=False, **kwargs)


def paired(seed=17, count=1000):
    x = np.random.default_rng(seed).normal(size=count)
    return np.column_stack((x, 2*x+10, -3*x-4))


class CorrelationChangeTests(unittest.TestCase):
    def test_full_signed_ccf_matches_pearson_for_every_lag(self):
        data = paired()
        data[:, 1] += np.random.default_rng(91).normal(0, .2, len(data))
        detector = CorrelationDetector(persistence=1).fit(data[:128])
        result = detector.analyze(data[128:160])
        for pair in result.pairs:
            i, j = pair.pair
            for lag, actual in zip(range(-4, 5), pair.ccf):
                block = data[128:160]
                x, y = (block[:-lag, i], block[lag:, j]) if lag>0 else (
                    (block[-lag:, i], block[:lag, j]) if lag<0 else (block[:, i],block[:, j]))
                self.assertAlmostEqual(actual, pearsonr(x, y).statistic, places=12)
            self.assertEqual(pair.reference_lag, 0)
        self.assertTrue(result.ready)
        self.assertEqual(result.score, 0)

    def test_common_steps_trends_bursts_and_cycle_changes_preserve_relationship(self):
        train = paired()[:128]
        t = np.arange(400)
        signals = [np.where(t<120,0.,100.), t.astype(float),
                   np.where((t>=100)&(t<150),1000.,0.)+np.sin(t),
                   np.sin(t/4), np.sin(t/20), np.ones(400)]
        for x in signals:
            with self.subTest(signal=x[:4]):
                values = np.column_stack((x, 2*x+10, -3*x-4))
                self.assertEqual(max(CorrelationDetector().fit(train).detect_series(values)), 0)

    def test_decorrelation_and_sign_reversal_are_detected(self):
        train = paired()
        rng = np.random.default_rng(9)
        for changed in (np.column_stack((train[:,0], rng.normal(size=1000))),
                        np.column_stack((train[:,0], -2*train[:,0]+10))):
            detector = CorrelationDetector().fit(train[:128,:2])
            results = detector.replay(changed)
            self.assertGreater(max(r.score for r in results), .8)
            self.assertTrue(results[-1].detected)
            self.assertEqual(results[-1].affected_series, (0,1))

    def test_reference_delay_is_fixed_and_changed_delay_is_detected(self):
        x = np.random.default_rng(51).normal(size=1200)
        data = np.column_stack((x[8:], x[5:-3]))  # j follows i by three points
        detector = CorrelationDetector(window=64,max_lag=8).fit(data[:128])
        stable = detector.replay(data[128:500])
        self.assertEqual(max(r.score for r in stable), 0)
        self.assertEqual(stable[-1].pairs[0].reference_lag, 3)
        changed = np.column_stack((x[508:], x[501:-7]))
        result = detector.analyze(changed)
        self.assertGreater(result.score, .6)
        self.assertEqual(result.pairs[0].reference_lag, 3)
        self.assertEqual(np.argmax(np.abs(result.pairs[0].ccf))-8, 7)

    def test_loss_of_one_variable_and_common_flat_have_distinct_semantics(self):
        detector = CorrelationDetector().fit(paired()[:128,:2])
        both = detector.analyze([[5.,99.]]*50)
        self.assertEqual(both.score, 0)
        self.assertTrue(both.pairs[0].uninformative)
        self.assertIsNone(both.pairs[0].correlation)
        x = np.arange(50)
        one = detector.analyze(np.column_stack((x,np.ones(50))))
        self.assertEqual(one.score, 1)
        self.assertFalse(one.pairs[0].uninformative)
        self.assertIsNone(one.pairs[0].correlation)

    def test_independent_affine_coordinate_changes_leave_scores_unchanged(self):
        data = paired()
        data[350:550,1] = np.random.default_rng(7).normal(size=200)
        expected = CorrelationDetector().fit(data[:128]).detect_series(data[128:])
        transformed = data * [-.001, 1e8, 7.] + [1e3,-2e9,100]
        actual = CorrelationDetector().fit(transformed[:128]).detect_series(transformed[128:])
        np.testing.assert_allclose(actual, expected, atol=1e-9)

    def test_recovery_and_frozen_background(self):
        data = paired()
        detector = CorrelationDetector().fit(data[:128])
        changed = data[128:400].copy()
        changed[:,1] = 1
        for row in changed:
            detector.update(row)
        self.assertEqual(detector.result.score, 1)
        reference = detector.result.pairs[0].reference_ccf
        for row in data[400:500]:
            detector.update(row)
        self.assertEqual(detector.result.score, 0)
        self.assertEqual(detector.result.pairs[0].reference_ccf, reference)

    def test_unfitted_warmup_uses_disjoint_reference_and_current_window(self):
        data = paired()
        detector = CorrelationDetector(window=16,max_lag=2,reference_window=32)
        results = [detector.update(row) for row in data[:48]]
        self.assertFalse(any(r.ready for r in results[:47]))
        self.assertTrue(results[47].ready)
        detector.reset()
        self.assertFalse(detector.reference_ready)

    def test_fitted_reset_and_batch_do_not_reuse_or_change_active_stream(self):
        data = paired()
        detector = CorrelationDetector().fit(data[:128])
        for row in data[128:200]:
            detector.update(row)
        saved = detector.result
        first = detector.replay(data[200:300])
        self.assertEqual(detector.result, saved)
        self.assertEqual(first, detector.replay(iter(data[200:300])))
        fresh = CorrelationDetector().fit(data[:128])
        self.assertEqual(first, [fresh.update(row) for row in data[200:300]])
        detector.reset()
        self.assertTrue(detector.reference_ready)
        self.assertFalse(detector.ready)
        self.assertEqual(detector.detect([]), 0)

    def test_failed_fit_or_update_is_atomic(self):
        data = paired()
        detector = CorrelationDetector().fit(data[:128])
        for row in data[128:180]:
            detector.update(row)
        saved = detector.result
        for value in ([1,2], [True,2,3], [np.nan,2,3], [np.inf,2,3], 2, ['1',2,3]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                detector.update(value)
            self.assertEqual(detector.result, saved)
        for reference in ([], [[1,2]]*50, [[1,2,3],[2,3]], [[1,np.nan]]*128):
            with self.subTest(reference=reference[:2]), self.assertRaises(ValueError):
                detector.fit(reference)
            self.assertEqual(detector.result, saved)
        self.assertEqual(detector.update(data[180]).score, 0)

    def test_unrelated_pairs_are_excluded_and_empty_reference_is_rejected(self):
        data = paired()
        data[:,2] = np.random.default_rng(918).normal(size=1000)
        result = CorrelationDetector().fit(data[:128]).analyze(data[128:200])
        self.assertEqual([p.pair for p in result.pairs], [(0,1)])
        with self.assertRaisesRegex(ValueError, 'no sufficiently correlated pair'):
            CorrelationDetector().fit(data[:128,1:])

    def test_persistence_has_exact_moving_mean_semantics(self):
        detector = CorrelationDetector(window=8,max_lag=0,persistence=3).fit(paired()[:128,:2])
        results = detector.replay(np.column_stack((np.arange(10),np.ones(10))))
        np.testing.assert_allclose([r.score for r in results], [0]*7+[1/3,2/3,1])

    def test_noise_floor_suppresses_near_flat_windows_but_keeps_divergence(self):
        data = paired()
        detector = CorrelationDetector(noise_floor=.1).fit(data[:128,:2])
        rng = np.random.default_rng(7)
        quiet = 100+rng.normal(0,.01,(100,2))
        self.assertEqual(max(detector.detect_series(quiet)), 0)
        changed = np.column_stack((np.arange(100),quiet[:,0]))
        self.assertEqual(detector.analyze(changed).score, 1)

    def test_reference_is_bounded_and_retains_only_healthy_tail(self):
        data = paired()
        long = CorrelationDetector().fit(iter(data[:500])).analyze(data[500:600])
        short = CorrelationDetector().fit(data[372:500]).analyze(data[500:600])
        self.assertEqual(long, short)

    def test_extreme_values_and_small_variation_at_large_level(self):
        for x in (np.linspace(-1,1,64)*1e308,
                  np.tile([-1e308,1e308],32), 1e15+np.arange(64)):
            result = CorrelationDetector().fit(np.column_stack((x,-x))).analyze(np.column_stack((x,-x)))
            self.assertTrue(result.ready)
            self.assertEqual(result.score, 0)

    def test_validation(self):
        cases = [{'window':True},{'window':7},{'max_lag':-1},{'max_lag':9},
                 {'reference_window':16},{'persistence':0},{'min_correlation':0},
                 {'min_correlation':1.1},{'min_change':1},{'min_change':np.nan},
                 {'alert_threshold':0},{'alert_threshold':np.inf},
                 {'noise_floor':-1},{'noise_floor':np.nan}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CorrelationDetector(**kwargs)


if __name__ == '__main__':
    unittest.main()
