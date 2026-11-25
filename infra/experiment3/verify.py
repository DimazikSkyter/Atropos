"""Independently verify a saved experiment run, including baseline counter deltas."""
import argparse
import datetime as dt
import json
from pathlib import Path
import urllib.parse
import urllib.request

def verify(folder, baseline=300, rps=750):
    path = Path(folder)
    previous = json.loads((path / 'verification.json').read_text())
    start = previous['started_at']
    records = [json.loads(line) for line in (path / 'metrics.ndjson').read_text().splitlines()]
    states = json.loads((path / 'docker-state.json').read_text())
    summary = json.loads((path / 'k6-summary.json').read_text())['metrics']
    logs = (path / 'service1.log').read_text()
    gc = (path / 'service1-gc.log').read_text()
    def values(name, service='service1', subset=None):
        return [(r['time'],float(line.rsplit(' ',1)[1])) for r in (records if subset is None else subset)
            for line in r.get(service,'').splitlines() if line.startswith(name+'{')]
    stable = [r for r in records if 60 < r['time']-start < baseline-10]
    def delta(name):
        series=values(name,subset=stable)
        if len(series)<2: raise ValueError('Insufficient baseline samples: '+name)
        return series[-1][1]-series[0][1]
    timeouts=delta('exp3_timeouts_total'); received=delta('exp3_received_total')
    stable_queue=max(v for _,v in values('exp3_queue_depth',subset=stable))
    state=states['service1']['State']
    is_oom=state['OOMKilled'] or (state['ExitCode']!=0 and 'java.lang.OutOfMemoryError' in logs)
    oom_time=dt.datetime.fromisoformat(state['FinishedAt'].replace('Z','+00:00')).timestamp() if is_oom else None
    requests=summary.get('http_reqs',{}).get('count',0)
    dropped=summary.get('dropped_iterations',{}).get('count',0)
    # The requested experiment ends at OOM. Keep post-OOM generator diagnostics,
    # but verify the offered rate and dropped iterations over the live experiment.
    audit_path=path/'load-until-oom.json'
    if audit_path.exists(): load_audit=json.loads(audit_path.read_text())
    else:
        load_audit={}
        queries={
            'offered_rps':'sum(rate(k6_http_reqs_total{experiment="experiment3",name="submit"}[10s]))',
            'dropped':'sum(k6_dropped_iterations_total{experiment="experiment3"})',
        }
        for name,query in queries.items():
            params=urllib.parse.urlencode({'query':query,'start':start+15,'end':oom_time,'step':'2s'})
            with urllib.request.urlopen('http://localhost:8428/api/v1/query_range?'+params,timeout=15) as response:
                load_audit[name]=json.load(response)
        audit_path.write_text(json.dumps(load_audit,indent=2))
    def audit_values(name):
        return [float(v) for series in load_audit[name]['data']['result'] for _,v in series.get('values',[])]
    rates=audit_values('offered_rps');drops_before=audit_values('dropped')
    offered=sum(rates)/len(rates) if rates else 0
    checks={
        'service1_confirmed_oom': is_oom,
        'oom_after_baseline_before_600s': oom_time is not None and baseline < oom_time-start <= 600,
        'service2_survived': states['service2']['State']['Running'] and not states['service2']['State']['OOMKilled'],
        'k6_no_dropped_iterations_before_oom': max(drops_before or [0])==0,
        'k6_steady_offered_rate_until_oom': len(rates)>30 and rps*0.98<=offered<=rps*1.02,
        'baseline_timeout_fraction_below_1pct': received>0 and timeouts/received<0.01,
        'stable_queue_after_warmup': stable_queue<500,
        'queue_grew': max(v for _,v in values('exp3_queue_depth'))>10000,
        'retries_occurred': max(v for _,v in values('exp3_retries_total'))>1000,
        'degraded_phase_observed': max(v for _,v in values('exp3_degraded','service2'))==1,
        'minor_gc_observed': 'Pause Young' in gc,
        'major_gc_observed': 'Pause Full' in gc,
    }
    result={**previous,'checks':checks,
        'oom_after_seconds': None if oom_time is None else oom_time-start,
        'oom_kind':'cgroup' if state['OOMKilled'] else 'JVM heap' if is_oom else None,
        'baseline_timeouts':timeouts,'baseline_received':received,
        'baseline_window_seconds':[stable[0]['time']-start,stable[-1]['time']-start],
        'baseline_timeout_fraction':timeouts/max(received,1),'max_baseline_queue':stable_queue,
        'requests_in_full_recording':requests,'dropped_iterations_in_full_recording':dropped,
        'offered_rps_until_oom':offered,'offered_rate_samples':len(rates),
        'dropped_iterations_until_oom':max(drops_before or [0]),
        'verification_scope':'Start → confirmed OOM; ten minutes is the maximum wait, not required load after container exit.',
        'grafana_url':f'http://localhost:3001/d/experiment3-retry-oom?from={int(start*1000)}&to={int((oom_time+15)*1000)}' if oom_time else previous['grafana_url'],
        'minor_gc_count':sum('Pause Young' in line for line in gc.splitlines()),
        'major_gc_count':sum('Pause Full' in line for line in gc.splitlines()),
        'verification_method':'Independent audit of saved metrics, Docker state, JVM log, GC log and k6 summary; excludes first 60 s of JVM warmup from baseline rate.'}
    if not (path/'verification-initial.json').exists():
        (path/'verification-initial.json').write_text(json.dumps(previous,indent=2))
    (path/'verification.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
    if not all(checks.values()): raise SystemExit('Verification failed')
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder');p.add_argument('--baseline',type=int,default=300);p.add_argument('--rps',type=int,default=750)
    args=p.parse_args();verify(args.folder,args.baseline,args.rps)
