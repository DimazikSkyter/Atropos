"""Check the displayed moving average against all 60-point arithmetic means."""
import json
import math
import statistics
import time
from datetime import datetime
from pathlib import Path
import publish_verify as replay

ROOT=Path(__file__).resolve().parent
samples,meta,_,_=replay.local_checks()
expected_raw={int(t.timestamp()):v for t,v in samples}
expected_mean={int(samples[i][0].timestamp()):statistics.fmean(v for _,v in samples[i-59:i+1]) for i in range(59,len(samples))}
deadline=time.monotonic()+40
while True:
    dashboard=json.loads(replay.request(replay.GRAFANA+'/api/dashboards/uid/'+meta['dashboard_uid'],headers=replay.AUTH))['dashboard']
    panel=next(p for p in dashboard['panels'] if p['id']==1)
    if any(t['refId']=='B' for t in panel['targets']):break
    if time.monotonic()>=deadline:raise AssertionError('Grafana has not provisioned the average line')
    time.sleep(2)
payload={'from':str(min(expected_raw)*1000),'to':str(max(expected_raw)*1000),
         'queries':[{**t,'intervalMs':15000,'maxDataPoints':10000} for t in panel['targets']]}
results=json.loads(replay.request(replay.GRAFANA+'/api/ds/query',json.dumps(payload).encode(),{**replay.AUTH,'Content-Type':'application/json'}))['results']
checks={}
for ref,expected in [('A',expected_raw),('B',expected_mean)]:
    result=results[ref]
    assert not result.get('error'),result.get('error')
    actual={}
    for frame in result['frames']:
        fields=frame['schema']['fields']
        ti=next(i for i,f in enumerate(fields) if f['type']=='time')
        vi=next(i for i,f in enumerate(fields) if f['type']=='number')
        times,values=frame['data']['values'][ti],frame['data']['values'][vi]
        actual.update((int(t/1000),v) for t,v in zip(times,values) if v is not None)
    checks[ref+'_matches_all_expected_values']=set(actual)==set(expected) and all(math.isclose(actual[t],v,abs_tol=1e-8) for t,v in expected.items())
previous=json.loads((ROOT/'verification.json').read_text(encoding='utf-8'))['after']
current=replay.summary(replay.database())
checks['all_database_measurements_unchanged']=all(current.get(name)==info for name,info in previous.items())
overview=json.loads(replay.request(replay.GRAFANA+'/api/dashboards/uid/influxdb-replay',headers=replay.AUTH))['dashboard']
checks['average_also_in_overview']=any(p.get('title')==meta['dashboard_title'] and any(t.get('refId')=='B' for t in p.get('targets',[])) for p in overview['panels'])
report={'passed':all(checks.values()),'checks':checks,'mean_window_minutes':15,'samples_per_window':60,
        'raw_points':len(expected_raw),'mean_points':len(expected_mean),'overall_mean_ms':statistics.fmean(expected_raw.values()),
        'moving_mean_min_ms':min(expected_mean.values()),'moving_mean_max_ms':max(expected_mean.values())}
(ROOT/'mean-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report,indent=2))
raise SystemExit(0 if report['passed'] else 1)
