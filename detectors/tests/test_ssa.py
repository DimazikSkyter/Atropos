import math
import unittest
from dataclasses import FrozenInstanceError

import numpy as np

from apps.detectors import SSADetector, SSAResult


CONFIG = dict(window=64, data_max_size=256, lag=16, refit_interval=8)


def shape_change(seed=43):
    rng = np.random.default_rng(seed)
    t = np.arange(2000)
    p = 2*np.pi*t/32
    base = np.sin(p)+.35*np.sin(2*p+.3)
    changed = np.sin(p)+.8*np.sin(3*p+.5)
    changed *= np.std(base[:480])/np.std(changed[:480])
    return np.where(t < 800, base, changed)+rng.normal(0,.04,len(t))


class SSATests(unittest.TestCase):
    def test_projection_and_score_match_independent_numpy_svd(self):
        rng = np.random.default_rng(123)
        x = 23 + np.sin(np.arange(330)/4) + rng.normal(0,.1,330)
        detector = SSADetector(**{**CONFIG,'refit_interval':1})
        for i,v in enumerate(x):
            detector.update(v)
            if not detector.ready:continue
            ref = x[i-255:i-63]
            current = x[i-63:i+1]
            center, sigma = ref.mean(), ref.std()
            A = np.lib.stride_tricks.sliding_window_view(ref-center,16)
            _,s,Vt = np.linalg.svd(A,full_matrices=False)
            rank = min(int(np.searchsorted(np.cumsum(s*s),.95*np.sum(s*s))+1),12,15)
            U = Vt[:rank].T
            B = np.lib.stride_tricks.sliding_window_view(current-center,16)
            rms = np.sqrt(np.mean((B-(B@U)@U.T)**2))
            expected = rms/sigma
            r = detector.result
            self.assertEqual(r.rank,rank)
            self.assertAlmostEqual(r.reference_sigma,sigma,places=12)
            self.assertAlmostEqual(r.residual_rms,rms,places=12)
            self.assertAlmostEqual(r.deformation,expected,places=12)
            self.assertAlmostEqual(r.score,expected/(1+expected),places=12)

    def test_reference_never_contains_current_window_even_between_refits(self):
        detector = SSADetector(**CONFIG)
        for i,v in enumerate(shape_change()[:450]):
            detector.update(v)
            if not detector.ready:continue
            r = detector.result
            self.assertLessEqual(r.reference_end,i-detector.window)
            self.assertEqual(r.reference_end,i-detector.window-r.refit_age)
            self.assertLess(r.refit_age,detector.refit_interval)
            self.assertEqual(r.reference_size,192)

    def test_current_change_does_not_inflate_its_own_reference_sigma(self):
        rng = np.random.default_rng(42)
        reference = rng.normal(0,1,192)
        small = np.r_[reference,rng.normal(0,1,64)]
        big = small.copy();big[-64:] *= 50
        a,b = (SSADetector(**CONFIG).analyze(v) for v in (small,big))
        self.assertEqual(a.reference_sigma,b.reference_sigma)
        self.assertEqual(a.reference_deformation,b.reference_deformation)
        self.assertGreater(b.deformation,20*a.deformation)
        self.assertGreater(b.score,a.score)

    def test_exact_warmup_and_last_observation_are_preserved(self):
        detector = SSADetector(**CONFIG)
        self.assertEqual(detector.result,SSAResult())
        for _ in range(255):
            self.assertEqual(detector.update(0.),0.)
            self.assertFalse(detector.ready)
        self.assertEqual(detector.update(0.),0.)
        self.assertTrue(detector.ready)
        self.assertEqual(detector.update(1.),1.)
        self.assertEqual(len(detector.detect_single_seria([0.]*256+[1.])),257)

    def test_causal_scores_do_not_depend_on_future(self):
        x = shape_change()
        for interval in (1,8):
            detector = SSADetector(**{**CONFIG,'refit_interval':interval})
            prefix = detector.detect_single_seria(x[:930])
            extended = detector.detect_single_seria(np.r_[x[:930],1e12,-1e12])
            np.testing.assert_array_equal(prefix,extended[:len(prefix)])

    def test_batch_stream_final_diagnostics_and_alias_agree(self):
        x = shape_change()[:930]
        stream = SSADetector(**CONFIG)
        scores = [stream.update(v) for v in x]
        batch = SSADetector(**CONFIG)
        self.assertEqual(batch.detect_single_seria(iter(x)),scores)
        self.assertEqual(batch.detect(x),scores[-1])
        self.assertEqual(batch.score(x),scores[-1])
        self.assertEqual(batch.analyze(x),stream.result)

    def test_batch_does_not_mutate_active_stream_and_reset_rewarms(self):
        detector = SSADetector(**CONFIG)
        for v in shape_change()[:450]:detector.update(v)
        state = (tuple(detector._values),detector.result,detector._index,detector._fitted_at)
        detector.detect_single_seria([0.]*300)
        detector.analyze([1.]*300)
        self.assertEqual(state,(tuple(detector._values),detector.result,detector._index,detector._fitted_at))
        self.assertEqual(detector.detect([]),0.)
        self.assertEqual(detector.detect_single_seria([]),[])
        detector.reset()
        self.assertFalse(detector.ready)
        self.assertEqual(detector.result,SSAResult())
        self.assertEqual(detector.update(1.),0.)

    def test_scale_parameter_changes_only_bounded_mapping(self):
        x = shape_change()[:850]
        a = SSADetector(**CONFIG).analyze(x)
        self.assertGreater(a.deformation,0)
        b = SSADetector(**CONFIG,score_scale=a.deformation).analyze(x)
        c = SSADetector(**CONFIG,score_scale=3).analyze(x)
        self.assertEqual(a.deformation,b.deformation)
        self.assertEqual(b.score,.5)
        self.assertAlmostEqual(c.score,a.deformation/(a.deformation+3),places=14)
        self.assertLess(c.score,a.score)

    def test_new_constant_level_eventually_becomes_new_norm(self):
        x = np.r_[np.zeros(300),np.ones(700)*4]
        scores = np.asarray(SSADetector(**CONFIG).detect_single_seria(x))
        self.assertEqual(scores[:300].max(),0)
        self.assertEqual(scores[300:340].max(),1)
        self.assertEqual(scores[600:].max(),0)

    def test_frozen_reference_retains_departure_and_batch_preserves_mode(self):
        detector=SSADetector(**{**CONFIG,'refit_interval':None})
        x=np.r_[np.zeros(300),np.ones(700)*4]
        scores=[detector.update(v) for v in x]
        self.assertEqual(scores,detector.detect_single_seria(x))
        self.assertEqual(scores[300:],[1.]*700)
        self.assertEqual(detector.result.reference_end,191)
        self.assertEqual(detector.result.refit_age,len(x)-256)

    def test_frozen_model_still_scores_causally_with_bounded_memory(self):
        detector=SSADetector(**{**CONFIG,'refit_interval':None})
        x=shape_change()
        prefix=detector.detect_single_seria(x[:900])
        self.assertEqual(prefix,detector.detect_single_seria(np.r_[x[:900],1e9])[:900])
        for v in x:detector.update(v)
        self.assertEqual(len(detector._values),detector.data_max_size)

    def test_shape_change_reacts_then_adapts_to_stable_new_shape(self):
        for seed in (11,43,55):
            with self.subTest(seed=seed):
                scores = np.asarray(SSADetector(**CONFIG).detect_single_seria(shape_change(seed)))
                self.assertLess(scores[400:700].mean(),.15)
                self.assertGreater(scores[800:900].max(),.25)
                self.assertLess(scores[1400:].mean(),.15)

    def test_dispersion_jump_retains_initial_response_and_later_adapts(self):
        rng = np.random.default_rng(43)
        x = np.r_[rng.normal(0,.1,500),rng.normal(0,3,700)]
        a = np.asarray(SSADetector(window=64,data_max_size=256,lag=32).detect_single_seria(x))
        self.assertGreater(a[500:565].max(),.85)
        self.assertLess(abs(a[900:].mean()-a[300:450].mean()),.1)

    def test_autolag_uses_reference_period_and_constant_fallback(self):
        t = np.arange(384)
        r = SSADetector(window=96,data_max_size=384).analyze(np.sin(2*np.pi*t/32))
        self.assertEqual(r.lag,32)
        self.assertEqual(r.rank,2)
        r = SSADetector(window=96,data_max_size=384).analyze(np.ones(384))
        self.assertEqual(r.lag,32)
        self.assertEqual(r.rank,0)

    def test_scale_translation_sign_invariance(self):
        x = shape_change()[:950]
        detector = SSADetector(**CONFIG)
        expected = detector.detect_single_seria(x)
        for changed in (x+473.6,x*100,-x,x*1e-100,x*1e100):
            np.testing.assert_allclose(detector.detect_single_seria(changed),expected,rtol=1e-10,atol=1e-10)

    def test_constant_and_extreme_finite_inputs_keep_diagnostics_finite(self):
        scenarios = ([1e308]*400,[1e-320]*400,[1e308,-1e308]*200,
                     [0.]*256+[1e-320]*400,[1e308]*256+[-1e308]*400)
        for x in scenarios:
            with self.subTest(first=x[:2]):
                detector = SSADetector(**CONFIG)
                for v in x:
                    score = detector.update(v)
                    self.assertTrue(math.isfinite(score) and 0<=score<=1)
                    if detector.ready:
                        for value in (detector.result.deformation,detector.result.residual_rms,
                                      detector.result.reference_sigma,detector.result.reference_deformation):
                            self.assertTrue(math.isfinite(value))

    def test_invalid_settings_are_rejected(self):
        for kw in ({'window':7},{'window':True},{'window':8.5},{'data_max_size':23},
                   {'window':64,'data_max_size':128},{'lag':True},{'lag':1},{'lag':97},
                   {'lag':3.5},{'energy':0},{'energy':1},{'energy':math.nan},
                   {'max_rank':0},{'max_rank':1.5},{'refit_interval':False},
                   {'score_scale':0},{'score_scale':math.inf},{'score_scale':True}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):SSADetector(**kw)

    def test_invalid_samples_do_not_advance_state(self):
        detector = SSADetector(**CONFIG)
        for v in shape_change()[:300]:detector.update(v)
        before = (tuple(detector._values),detector.result,detector._index,detector._fitted_at)
        for value in (math.nan,math.inf,-math.inf,True,None,'1',[1]):
            with self.subTest(value=value),self.assertRaises(ValueError):detector.update(value)
            self.assertEqual(before,(tuple(detector._values),detector.result,detector._index,detector._fitted_at))

    def test_state_is_bounded_and_results_are_immutable(self):
        detector = SSADetector(**CONFIG,max_rank=3)
        for v in shape_change():detector.update(v)
        self.assertEqual(len(detector._values),detector.data_max_size)
        self.assertEqual(detector._model['basis'].shape,(16,3))
        with self.assertRaises(FrozenInstanceError):detector.result.score=0

    def test_known_phase_only_harmonic_space_limitation(self):
        t = np.arange(2000);p=2*np.pi*t/32
        base=np.sin(p)+.35*np.sin(2*p+.3)
        changed=np.sin(p)+.35*np.sin(2*p+2.4)
        # The reference is still purely old here, but both use the same span.
        x=np.r_[base[:800],changed[800:]]
        r=SSADetector(window=64,data_max_size=768,lag=32,energy=.999).analyze(x[:864])
        self.assertLess(r.deformation,1e-12)


if __name__=='__main__':unittest.main(verbosity=2)
