from dataclasses import replace
from datetime import timedelta
import csv,json,unittest
import numpy as np

from apps.detectors import ACFDetector
from tests.acf_dispatcher_report import (CONFIGS,FIXTURES,START_MARK,END_MARK,load_cases,
                                        replay,summarize,stamp,merge_appendix)


class DispatcherACFTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases,cls.meta=load_cases()
        cls.results={c.host:{key:replay(c,cfg) for key,cfg in CONFIGS.items()} for c in cls.cases}

    def test_five_native_series_and_exact_selected_grid(self):
        self.assertEqual([c.host for c in self.cases],['host1','host4','host6','host7','host8'])
        self.assertEqual(sum(len(c.values) for c in self.cases),2105)
        for c in self.cases:
            self.assertEqual(len(c.values),421)
            self.assertEqual(c.focus.sum(),141)
            times=[t for t,use in zip(c.times,c.focus) if use]
            self.assertEqual(times[0].strftime('%H:%M:%S'),'18:17:40')
            self.assertEqual(times[-1].strftime('%H:%M:%S'),'18:29:20')
            self.assertTrue(all((b-a).total_seconds()==5 for a,b in zip(c.times,c.times[1:])))

    def test_grafana_frames_equal_selected_influx_values(self):
        frames=json.loads((FIXTURES/'grafana-focus.json').read_text(encoding='utf-8'))['results']['A']['frames']
        actual=[]
        for frame in frames:
            fields=frame['schema']['fields'];data=frame['data']['values']
            ti=next(i for i,f in enumerate(fields) if f['type']=='time')
            vi=next(i for i,f in enumerate(fields) if f['type']=='number')
            host=fields[vi]['labels']['host']
            actual.extend((host,int(t),float(v)) for t,v in zip(data[ti],data[vi]))
        expected=[(c.host,int(t.timestamp()*1000),float(v)) for c in self.cases
                  for t,v,use in zip(c.times,c.values,c.focus) if use]
        self.assertEqual(sorted(actual),sorted(expected))
        self.assertTrue(self.meta['grafana_frames_equal_influx'])

    def test_reference_is_each_hosts_own_52_pre_growth_values(self):
        backgrounds=[]
        for c in self.cases:
            values=c.values[c.reference]
            self.assertEqual(len(values),52)
            self.assertTrue(np.all((values>=.010)&(values<=.020)))
            r=self.results[c.host]['short'][1][-1]
            z=values-values.mean();den=z@z
            expected=[float(z[k:]@z[:len(z)-k]/den) for k in range(11)]
            np.testing.assert_allclose(r.reference_acf,expected,atol=1e-13)
            backgrounds.append(r.reference_acf)
        self.assertFalse(all(np.allclose(backgrounds[0],b) for b in backgrounds[1:]))

    def test_no_current_window_is_scored_using_future_baseline(self):
        for c in self.cases:
            for key,cfg in CONFIGS.items():
                rows,_=self.results[c.host][key]
                scored=[r for r in rows if r['ready']]
                expected=c.reference_stop+timedelta(seconds=cfg['window']*5)
                self.assertEqual(stamp(scored[0]['timestamp_msk']),expected)
                for r in rows:
                    if not r['ready']:self.assertIsNone(r['score']);self.assertIsNone(r['depth'])
                    else:self.assertGreater(stamp(r['timestamp_msk']),c.reference_stop)
                self.assertEqual(sum(r['in_focus'] and r['ready'] for r in rows),82 if key=='short' else 106)

    def test_numpy_oracle_uses_actual_reference_count_and_matches_peak(self):
        for c in self.cases:
            reference=c.values[c.reference]
            def profile(x,lag):
                z=x-x.mean();e=z@z
                return np.array([float(z[k:]@z[:len(z)-k]/e) if e else 0. for k in range(lag+1)])
            for key,cfg in CONFIGS.items():
                rows,snapshots=self.results[c.host][key]
                summary=summarize(c,cfg,rows,snapshots)
                i=summary['peak_index'];r=snapshots[i]
                ref=profile(reference,cfg['max_lag'])
                current=profile(c.values[i-cfg['window']+1:i+1],cfg['max_lag'])
                band=np.array([0.]+[min(2.,max(cfg['min_change'],cfg['threshold']*np.sqrt(
                    (1+2*np.sum(ref[1:k]**2))*(1/cfg['window']+1/52)))) for k in range(1,cfg['max_lag']+1)])
                diff=abs(current-ref)
                significant=[k for k in range(1,cfg['max_lag']+1) if diff[k]>band[k]]
                score=sum(min(1.,max(0.,diff[k]-band[k])) for k in range(1,cfg['max_lag']+1))/cfg['max_lag']
                np.testing.assert_allclose(r.acf,current,atol=1e-13)
                np.testing.assert_allclose(r.thresholds,band,atol=1e-13)
                self.assertAlmostEqual(r.score,score,places=13)
                self.assertEqual(r.depth,max(significant,default=0))

    def test_stream_prefix_including_training_does_not_depend_on_future(self):
        for c in self.cases:
            for stop in (80,200):
                prefix=replace(c,times=c.times[:stop],values=c.values[:stop])
                for key,cfg in CONFIGS.items():
                    rows,_=replay(prefix,cfg)
                    self.assertEqual(rows,self.results[c.host][key][0][:stop])

    def test_seconds_and_milliseconds_give_identical_acf_scores(self):
        c=self.cases[0]
        for key,cfg in CONFIGS.items():
            scaled=replace(c,values=c.values*1000.)
            rows,_=replay(scaled,cfg)
            for a,b in zip(rows,self.results[c.host][key][0]):
                if a['ready']:self.assertAlmostEqual(a['score'],b['score'],places=12)

    def test_metrics_are_only_for_requested_interval_and_depth_is_times_five(self):
        for c in self.cases:
            for key,cfg in CONFIGS.items():
                rows,snapshots=self.results[c.host][key]
                summary=summarize(c,cfg,rows,snapshots)
                valid=[r for r in rows if r['ready'] and r['in_focus']]
                self.assertEqual(summary['max_score'],max(r['score'] for r in valid))
                self.assertTrue(rows[summary['peak_index']]['in_focus'])
                for r in rows:
                    if r['ready']:
                        self.assertTrue(0<=r['score']<=1)
                        self.assertEqual(r['depth_seconds'],r['depth']*5)
                        self.assertTrue(0<=r['depth']<=cfg['max_lag'])

    def test_default_cannot_be_fitted_to_52_reference_values(self):
        with self.assertRaises(ValueError):ACFDetector().fit(self.cases[0].values[self.cases[0].reference])

    def test_appendix_preserves_original_bytes_and_is_idempotent(self):
        original='<html><body>Старый отчет\r\n</body></html>'.encode()
        section=(START_MARK+'<section>Новый</section>'+END_MARK).encode()
        actual=merge_appendix(original,section)
        self.assertEqual(actual.replace(b'\n'+section+b'\n',b''),original)
        self.assertEqual(merge_appendix(actual,section),actual)
        changed=(START_MARK+'<section>Обновлено</section>'+END_MARK).encode()
        updated=merge_appendix(actual,changed)
        self.assertEqual(updated.count(START_MARK.encode()),1)
        self.assertEqual(updated.replace(b'\n'+changed+b'\n',b''),original)


if __name__=='__main__':unittest.main(verbosity=2)
