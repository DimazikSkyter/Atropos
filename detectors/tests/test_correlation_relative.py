"""Relative interaction size, as requested: 15/400 is larger than 25/800."""
import unittest
import numpy as np

from apps.detectors import CorrelationDetector


def relative_case(level,amplitude):
    rng=np.random.default_rng(63)
    x=level+amplitude*rng.normal(size=320)
    y=x.copy()
    y[160:]=level+amplitude*rng.normal(size=160)
    return np.column_stack((x,y))


class CorrelationRelativeTests(unittest.TestCase):
    def test_smaller_absolute_difference_at_lower_level_has_larger_score(self):
        low,high=relative_case(400,15),relative_case(800,25)
        config={'window':24,'max_lag':3,'persistence':2,'alignment_tolerance':1}
        a=CorrelationDetector(**config).replay(low)
        b=CorrelationDetector(**config).replay(high)
        self.assertGreater(max(r.score for r in a[190:]),max(r.score for r in b[190:])+.01)
        # The correlation problem is otherwise identical up to affine units.
        strict_a=CorrelationDetector(**config,relative_change_scale=0).detect_series(low)
        strict_b=CorrelationDetector(**config,relative_change_scale=0).detect_series(high)
        np.testing.assert_allclose(strict_a,strict_b,atol=1e-10)

    def test_known_example_orders_relative_weights(self):
        detector=CorrelationDetector(relative_change_scale=.1)
        model=(600.,20.,600.,20.,1.)
        e_low,w_low=detector._relative_weight(np.full(8,400.),np.full(8,415.),model)
        e_high,w_high=detector._relative_weight(np.full(8,800.),np.full(8,825.),model)
        self.assertGreater(e_low,e_high)
        self.assertGreater(w_low,w_high)
        self.assertLess(e_low,.04)
        self.assertGreater(e_high,.03)

    def test_ordinary_affine_offset_and_gain_are_not_counted_as_new_disagreement(self):
        x=800+np.random.default_rng(7).normal(size=200)
        data=np.column_stack((x,2*x+100))
        rows=CorrelationDetector(alignment_tolerance=1).replay(data)
        self.assertEqual(max(r.score for r in rows),0)
        valid=[p.relative_error for r in rows if r.ready for p in r.pairs if p.relative_error is not None]
        self.assertTrue(valid)
        self.assertLess(max(valid),1e-12)

    def test_multiplicative_unit_changes_preserve_relative_score(self):
        data=relative_case(800,30)
        config={'window':24,'max_lag':3,'alignment_tolerance':1}
        a=CorrelationDetector(**config).detect_series(data)
        b=CorrelationDetector(**config).detect_series(data*[1000,.001])
        np.testing.assert_allclose(a,b,atol=1e-9)

    def test_large_divergence_still_crosses_the_alarm_threshold(self):
        rng=np.random.default_rng(70)
        x=800+rng.normal(0,5,350)
        y=x.copy()
        y[170:]=400+rng.normal(0,5,180)
        results=CorrelationDetector(alignment_tolerance=1).replay(np.column_stack((x,y)))
        self.assertGreater(max(r.score for r in results[200:]),.5)
        self.assertGreater(sum(r.detected for r in results[200:]),50)

    def test_zero_scale_disables_relative_weight_and_positive_scale_suppresses_small_gaps(self):
        data=relative_case(800,25)
        a=CorrelationDetector(relative_change_scale=0).replay(data)
        b=CorrelationDetector(relative_change_scale=.1).replay(data)
        self.assertGreater(max(r.score for r in a),max(r.score for r in b))
        self.assertTrue(all(p.relative_weight==1 for r in a for p in r.pairs))

    def test_near_zero_and_extreme_levels_remain_finite(self):
        for x in (np.sin(np.arange(100)/5),np.tile([-1e308,1e308],50)):
            data=np.column_stack((x,-x))
            rows=CorrelationDetector(alignment_tolerance=1).replay(data)
            self.assertTrue(all(np.isfinite(r.score) and 0<=r.score<=1 for r in rows))

    def test_relative_parameter_validation(self):
        for value in (-.1,np.nan,np.inf,True,'0.1'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                CorrelationDetector(relative_change_scale=value)


if __name__=='__main__':
    unittest.main()
