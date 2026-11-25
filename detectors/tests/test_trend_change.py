import math
import unittest

import numpy as np

from apps.detectors import TrendDetector, _TrendWindow
from tests.trend_change_scenarios import core_scenarios


class TrendChangeTests(unittest.TestCase):
    def test_rolling_fit_against_independent_numpy_with_eviction(self):
        rng=np.random.default_rng(771)
        values=1e4+np.arange(1300)*.2+rng.normal(0,.7,1300)
        window=_TrendWindow(37)
        for i,v in enumerate(values):
            window.append(float(v-values[0]))
            if i<36:
                continue
            y=values[i-36:i+1]-values[0]
            x=np.arange(37)-18
            b=np.dot(x,y)/np.dot(x,x)
            residual=y-np.mean(y)-b*x
            m,s,r=window.fit()
            self.assertAlmostEqual(m,np.mean(y),places=8)
            self.assertAlmostEqual(s,b,places=8)
            self.assertAlmostEqual(r,np.sqrt(np.dot(residual,residual)/35),places=6)

    def test_all_clean_bends_and_piecewise_onset(self):
        cases=[c for c in core_scenarios() if ('noise0' in c.name and c.group=='change') or c.name=='piecewise']
        for case in cases:
            with self.subTest(case=case.name):
                d=TrendDetector(**case.config)
                rows=[d.update(float(v)) for v in case.values]
                for event in case.changes:
                    alarms=[(i,r) for i,r in enumerate(rows) if event.start<=i<event.end
                            and r.detected and r.direction==event.direction
                            and r.confirmed_at>=event.start]
                    self.assertTrue(alarms)
                    i,r=alarms[0]
                    self.assertLessEqual(i-event.start,d.window+d.inertia_window+5)
                    self.assertLessEqual(abs(r.change_start-event.start),3)
                    self.assertLessEqual(r.change_start,r.confirmed_at)
                self.assertFalse(any(r.detected for r in rows[:case.changes[0].start]))

    def test_noise_cycles_steps_and_outliers_controls(self):
        for case in core_scenarios():
            if case.changes!=() or case.group not in ('control','cycle'):
                continue
            with self.subTest(case=case.name):
                d=TrendDetector(**case.config)
                self.assertFalse(any(d.update(float(v)).detected for v in case.values))

    def test_score_accumulates_and_adapts_not_a_permanent_alarm(self):
        d=TrendDetector()
        rows=[d.update(100+.2*max(0,i-400)) for i in range(900)]
        self.assertGreater(max(r.score for r in rows),.99)
        self.assertLessEqual(max(abs(b.score-a.score) for a,b in zip(rows,rows[1:])),1/d.inertia_window+1e-10)
        self.assertTrue(all(r.score==0 for r in rows[-100:]))
        self.assertTrue(any(0<r.score<.8 for r in rows))

    def test_horizontal_angle_and_time_value_units(self):
        for degrees,expected in [(0,True),(.999,True),(1,True),(-1,True),(1.001,False),(-1.001,False),(20,False)]:
            with self.subTest(degrees=degrees):
                # slope is normalized units/minute, sample interval 15 seconds.
                slope=math.tan(math.radians(degrees))
                d=TrendDetector(value_scale=100,sample_interval=15,time_scale=60)
                for i in range(300):
                    r=d.update(50+i*slope*25)
                self.assertEqual(r.horizontal,expected)
                self.assertAlmostEqual(r.angle,degrees,places=8)
                self.assertAlmostEqual(r.score,0,places=8)

    def test_prefix_causality_batch_and_reset(self):
        y=[100+.2*max(0,i-400) for i in range(900)]
        d=TrendDetector()
        rows=[d.update(v) for v in y]
        prefix=TrendDetector()
        self.assertEqual([prefix.update(v) for v in y[:425]],rows[:425])
        before=prefix.result
        self.assertEqual(prefix.detect(y),rows[-1])
        self.assertEqual(prefix.detect_single_seria(iter(y)),[r.score for r in rows])
        self.assertIs(prefix.result,before)
        prefix.reset()
        self.assertEqual([prefix.update(v) for v in y],rows)
        self.assertFalse(TrendDetector().detect([]).ready)

    def test_parameter_validation_and_input_atomicity(self):
        for kwargs in [{'window':True},{'window':5},{'window':6.5},{'data_max_size':40},
                       {'inertia_window':1},{'inertia_window':41},{'threshold':0},
                       {'threshold':math.nan},{'value_scale':0},{'sample_interval':-1},
                       {'time_scale':math.inf},{'angle_tolerance':90},{'noise_floor':0}]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                TrendDetector(**kwargs)
        d=TrendDetector()
        d.update(1)
        before=d.result
        for v in (True,None,'1',math.nan,math.inf,1e300):
            with self.assertRaises(ValueError):
                d.update(v)
            self.assertIs(d.result,before)
            self.assertEqual(d._index,0)

    def test_warmup_last_value_and_bounded_memory(self):
        d=TrendDetector()
        for i in range(10000):
            r=d.update(100+i*.01)
            self.assertEqual(r.ready,i>=d.data_max_size-1)
            if not r.ready:
                self.assertIsNone(r.horizontal)
            self.assertTrue(0<=r.score<=1)
        self.assertEqual(len(d._reference.values)+len(d._tail.values),d.data_max_size)
        self.assertEqual(len(d._memory),d.inertia_window)
        self.assertEqual(d._index,9999)

    def test_scale_invariance_and_large_constant_offset(self):
        y=np.maximum(np.arange(900)-400,0)*.2
        def replay(values,**cfg):
            d=TrendDetector(**cfg)
            return [d.update(float(v)) for v in values]
        base=replay(y)
        for factor in (1e-6,1e6):
            other=replay(y*factor,value_scale=factor)
            np.testing.assert_allclose([r.score for r in base],[r.score for r in other],atol=1e-7)
        offset=replay(y+1e12)
        first=lambda rows:next((i,r.change_start) for i,r in enumerate(rows) if r.detected)
        self.assertEqual(first(base),first(offset))

    def test_long_piecewise_cycle_needs_a_reference_covering_its_period(self):
        t=np.arange(3600,dtype=float)
        y=100+6*np.abs((t%600)/600-.5)
        short=TrendDetector()
        long=TrendDetector(window=40,data_max_size=1200)
        # A straight side and a corner of a long triangle are locally a real
        # piecewise trend. History covering the cycle supplies the missing context.
        self.assertTrue(any(short.update(float(v)).detected for v in y))
        self.assertFalse(any(long.update(float(v)).detected for v in y))

    def test_breakpoint_matches_exhaustive_independent_least_squares(self):
        rng=np.random.default_rng(119)
        x=np.arange(700,dtype=float)
        y=100+.1*x+.3*np.maximum(x-350,0)+rng.normal(0,.2,len(x))
        d=TrendDetector()
        for i,value in enumerate(y):
            r=d.update(float(value))
            if r.detected:
                first=i-d.data_max_size+1
                observed=y[first:i+1]
                local=np.arange(len(observed),dtype=float)
                candidates=[]
                for k in range(d.window,len(observed)-d.window//2+1):
                    design=np.column_stack((np.ones(len(local)),local,np.maximum(local-k,0)))
                    fitted=design@np.linalg.lstsq(design,observed,rcond=None)[0]
                    candidates.append((float(np.sum((observed-fitted)**2)),k))
                # The streaming fit also allows a knot between samples;
                # the independent oracle here searches integer knots only.
                self.assertLessEqual(abs(r.change_start-(first+min(candidates)[1])),1)
                break
        else:
            self.fail('expected a confirmed change')

    def test_fractional_bend_is_not_rejected_as_a_discontinuous_step(self):
        x=np.arange(800,dtype=float)
        for start in (350.2,350.4,350.6,350.8):
            with self.subTest(start=start):
                d=TrendDetector()
                result=None
                for value in 100+.1*x+.3*np.maximum(x-start,0):
                    r=d.update(float(value))
                    if r.detected:
                        result=r
                        break
                self.assertIsNotNone(result)
                self.assertEqual(result.change_start,round(start))


if __name__=='__main__':
    unittest.main()
