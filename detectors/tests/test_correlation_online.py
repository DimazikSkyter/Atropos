"""No labelled reference: causal startup, online interaction learning and stability."""
import unittest
import numpy as np

from apps.detectors import CorrelationDetector as LevelCorrelationDetector


def CorrelationDetector(*args,**kwargs):
    """Isolate the pure CCF contracts; level-relative behavior has its own suite."""
    kwargs.setdefault('relative_change_scale',0.)
    return LevelCorrelationDetector(*args,**kwargs)


def linked(count=500,seed=7):
    x=np.random.default_rng(seed).normal(size=count)
    return np.column_stack((x,2*x+10,-3*x-7))


class CorrelationOnlineTests(unittest.TestCase):
    def test_first_score_on_fifth_point_and_provisional_scores_are_not_hidden(self):
        data=linked(40)
        results=CorrelationDetector(window=12,max_lag=2).replay(data)
        self.assertEqual([r.ready for r in results[:6]],[False]*4+[True]*2)
        self.assertTrue(all(r.ready for r in results[4:]))
        self.assertTrue(all(r.provisional for r in results[4:11]))
        self.assertFalse(any(r.provisional for r in results[11:]))
        self.assertEqual(len(results),len(data))
        self.assertEqual(max(r.score for r in results),0)

    def test_common_level_steps_trends_and_cycles_have_zero_score_without_fit(self):
        t=np.arange(300)
        shapes=[t.astype(float),np.sin(t/5),np.where(t<90,1.,100.),
                np.where((t>70)&(t<120),100.,0.)+np.sin(t/7), np.ones(300)]
        for x in shapes:
            with self.subTest(shape=x[:3]):
                data=np.column_stack((x,2*x+10,-3*x-7))
                results=CorrelationDetector(window=12,max_lag=2).replay(data)
                self.assertTrue(all(r.ready for r in results[4:]))
                self.assertEqual(max(r.score for r in results),0)

    def test_unrelated_start_does_not_fail_and_later_relationship_is_acquired(self):
        detector=CorrelationDetector(window=16,max_lag=2)
        for row in [[1.,9.]]*50:
            detector.update(row)
        self.assertTrue(detector.ready)
        self.assertFalse(detector.reference_ready)
        for row in linked(100)[:,:2]:
            detector.update(row)
        self.assertTrue(detector.reference_ready)
        self.assertEqual(detector.result.score,0)

    def test_sign_reversal_and_decorrelation_detected_from_unlabelled_stream(self):
        data=linked(600)
        detector=CorrelationDetector(window=24,max_lag=3,persistence=2)
        normal=[detector.update(row) for row in data[:200,:2]]
        self.assertEqual(max(r.score for r in normal),0)
        changed=data[200:400,:2].copy()
        changed[:,1]=-2*changed[:,0]+10
        results=[detector.update(row) for row in changed]
        self.assertGreater(max(r.score for r in results),.95)
        self.assertTrue(results[-1].detected)
        # Broken relationship is not absorbed into its own reference.
        self.assertGreater(results[-1].pairs[0].reference_correlation,.9)
        recovered=[detector.update(row) for row in data[400:,:2]]
        self.assertEqual(recovered[-1].score,0)

    def test_reference_adapts_to_small_coherent_changes(self):
        rng=np.random.default_rng(44)
        x=rng.normal(size=1000)
        y=x+rng.normal(0,.15,len(x))
        data=np.column_stack((x,y))
        detector=CorrelationDetector(window=32,reference_window=128)
        for row in data[:400]:
            detector.update(row)
        before=detector.result.pairs[0].reference_correlation
        y[400:]=x[400:]+rng.normal(0,.55,len(x)-400)
        for row in np.column_stack((x[400:],y[400:])):
            detector.update(row)
        after=detector.result.pairs[0].reference_correlation
        self.assertLess(after,before-.03)
        self.assertGreater(after,.7)

    def test_stream_batch_prefix_and_future_mutation_are_identical(self):
        data=linked(160)
        data[90:,1]*=-1
        detector=CorrelationDetector(window=12,max_lag=2)
        batch=detector.replay(data)
        self.assertEqual(batch,[detector.update(row) for row in data])
        saved=detector.result
        for stop in (5,11,12,60,100):
            self.assertEqual(detector.replay(data[:stop]),batch[:stop])
        mutated=data.copy()
        mutated[75:]=9999
        self.assertEqual(detector.replay(mutated)[:75],batch[:75])
        self.assertEqual(detector.result,saved)

    def test_affine_units_leave_automatic_relative_floors_and_scores_invariant(self):
        data=linked(250)
        data[130:,1]*=-1
        config={'window':16,'max_lag':2}
        a=CorrelationDetector(**config).replay(data)
        transformed=data*[-.001,1000.,-7.]+[500,1e7,-20]
        b=CorrelationDetector(**config).replay(transformed)
        np.testing.assert_allclose([r.score for r in a],[r.score for r in b],atol=1e-8)
        self.assertEqual([r.ready for r in a],[r.ready for r in b])

    def test_prior_scale_does_not_include_current_spike(self):
        data=linked(80)
        detector=CorrelationDetector(window=12,max_lag=2)
        for row in data:
            detector.update(row)
        result=detector.update([1e6,-1e6,1e6])
        expected=tuple(.3*np.std(data[:,i]) for i in range(3))
        np.testing.assert_allclose(result.noise_floors,expected,atol=1e-12)

    def test_online_fit_is_optional_history_and_reset_replays_it(self):
        data=linked(160)
        detector=CorrelationDetector(window=12,max_lag=2).fit(data[:80])
        actual=[detector.update(row) for row in data[80:]]
        replay=detector.replay(data[80:])
        self.assertEqual(actual,replay)
        detector.reset()
        self.assertFalse(detector.ready)
        self.assertTrue(detector.reference_ready)
        self.assertEqual(actual,[detector.update(row) for row in data[80:]])

    def test_invalid_input_is_atomic_and_memory_stays_bounded(self):
        detector=CorrelationDetector(window=12,max_lag=2,reference_window=64)
        data=linked(200)
        for row in data[:100]:
            detector.update(row)
        snapshot=detector.result
        for row in ([1,2],[np.nan,2,3],[True,2,3],3):
            with self.assertRaises(ValueError):
                detector.update(row)
            self.assertEqual(detector.result,snapshot)
        for row in data[100:]:
            detector.update(row)
        self.assertEqual(len(detector._history),64)
        self.assertEqual(len(detector._profiles),3)
        self.assertEqual(len(detector._memory),detector.persistence)

    def test_online_validation_and_no_pair_is_not_an_error(self):
        for kwargs in ({'min_samples':3},{'min_samples':33},{'min_samples':True},
                       {'adaptive':1},{'relative_noise_floor':-1},
                       {'relative_noise_floor':1},{'relative_noise_floor':np.nan}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                CorrelationDetector(**kwargs)
        result=CorrelationDetector().analyze([[1,2]]*40)
        self.assertTrue(result.ready)
        self.assertEqual(result.score,0)
        self.assertFalse(any(p.monitored for p in result.pairs))

    def test_nonzero_delay_is_acquired_from_the_stream(self):
        x=np.random.default_rng(51).normal(size=800)
        data=np.column_stack((x[8:],x[5:-3]))
        detector=CorrelationDetector(window=64,max_lag=8)
        for row in data[:300]:
            detector.update(row)
        self.assertTrue(detector.reference_ready)
        self.assertEqual(detector.result.pairs[0].reference_lag,3)
        self.assertEqual(detector.result.score,0)


if __name__=='__main__':
    unittest.main()
