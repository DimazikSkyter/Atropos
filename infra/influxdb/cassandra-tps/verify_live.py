"""Verify the added TPS and exact preservation of the existing InfluxDB history."""
import argparse,base64,csv,hashlib,io,json,math,os,statistics
from datetime import datetime,timedelta
from pathlib import Path
import urllib.request,urllib.parse

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'infra'/'influxdb'/'cassandra-tps' if (ROOT/'infra'/'influxdb'/'cassandra-tps').exists() else ROOT
INFLUX=os.getenv('INFLUX_URL','http://localhost:8086').rstrip('/')
ORG=os.getenv('INFLUXDB_ORG','atropos')
BUCKET=os.getenv('INFLUXDB_BUCKET','metrics-replay')
TOKEN=os.getenv('INFLUXDB_TOKEN','atropos-local-replay-token')
AUTH={'Authorization':'Basic '+base64.b64encode((os.getenv('GRAFANA_ADMIN_USER','admin')+':'+os.getenv('GRAFANA_ADMIN_PASSWORD','admin')).encode()).decode()}

def request(url,data=None,headers=None):
    req=urllib.request.Request(url,data=data,headers=headers or {})
    with urllib.request.urlopen(req,timeout=30) as response:return response.read().decode()

def database():
    flux=f'from(bucket: "{BUCKET}") |> range(start: 0) |> filter(fn: (r) => r._field == "value")'
    response=request(INFLUX+'/api/v2/query?'+urllib.parse.urlencode({'org':ORG}),flux.encode(),
                     {'Authorization':'Token '+TOKEN,'Content-Type':'application/vnd.flux','Accept':'application/csv'})
    points={}; header=None
    for fields in csv.reader(io.StringIO(response)):
        if not fields or fields[0].startswith('#'):continue
        if '_time' in fields:header=fields;continue
        row=dict(zip(header,fields))
        if not row.get('_time'):continue
        tags=tuple(sorted((k,v) for k,v in row.items() if k not in
                          {'','result','table','_start','_stop','_time','_value','_field','_measurement'} and v))
        identity=(row['_measurement'],int(datetime.fromisoformat(row['_time'].replace('Z','+00:00')).timestamp()),tags)
        if identity in points:raise AssertionError('Duplicate returned identity')
        points[identity]=float(row['_value'])
    return points

def summary(points):
    return {metric:{'points':len(part),'sha256':hashlib.sha256(json.dumps(sorted(part),separators=(',',':')).encode()).hexdigest()}
            for metric in sorted({key[0] for key in points})
            for part in [[(key,value) for key,value in points.items() if key[0]==metric]]}

def local_checks():
    metadata=json.loads((DATA/'metadata.json').read_text(encoding='utf-8'))
    expected={}; series={}
    for row in csv.DictReader((DATA/'metrics.csv').open(encoding='utf-8')):
        dt=datetime.fromisoformat(row['timestamp']); value=float(row['cassandra_tps'])
        expected[('cassandra_tps',int(dt.timestamp()),(('trace',row['trace']),))]=value
        series.setdefault(row['trace'],[]).append((dt,value))
    partial=datetime.fromisoformat(metadata['partial_drop_time']); death=datetime.fromisoformat(metadata['zero_time'])
    first=datetime.fromisoformat(next(e['time'] for e in metadata['events'] if e['name']=='first_node_failure'))
    plateau2=datetime.fromisoformat(next(e['time'] for e in metadata['events'] if e['name']=='node2_plateau'))
    def average(name,begin,end):
        vals=[v for t,v in series[name] if begin<=t<end]
        return statistics.fmean(vals)
    stats={}
    for name,points in series.items():
        before=[v for t,v in points if t<partial]
        after=[v for t,v in points if partial<=t<death]
        stats[name]={'baseline_mean_tps':statistics.fmean(before),'baseline_sigma_tps':statistics.pstdev(before),
                     'partial_mean_tps':statistics.fmean(after),'partial_sigma_tps':statistics.pstdev(after),
                     'partial_min_tps':min(after),'partial_max_tps':max(after),
                     'mean_ratio_around_first_failure':average(name,first,first+timedelta(minutes=30))/average(name,first-timedelta(minutes=30),first),
                     'last_before_zero':next(v for t,v in reversed(points) if t<death)}
    checks={
        'seven_traces':set(series)==set(metadata['traces']) and len(series)==7,
        'point_count_matches':len(expected)==metadata['total_points'],
        'uniform_15_second_step':all((b[0]-a[0]).total_seconds()==15 for points in series.values() for a,b in zip(points,points[1:])),
        'all_values_nonnegative_finite':all(math.isfinite(v) and v>=0 for v in expected.values()),
        'first_node_failure_no_tps_level_step':all(abs(s['mean_ratio_around_first_failure']-1)<.05 for s in stats.values()),
        'growing_response_time_does_not_collapse_tps':all(v>550 for t,v in series['green'] if t<partial)
                                                    and all(v>400 for t,v in series['yellow'] if t<partial),
        'node2_plateau_alone_no_tps_step':abs(average('green',plateau2,plateau2+timedelta(minutes=15))/average('green',plateau2-timedelta(minutes=15),plateau2)-1)<.05,
        'both_plateaus_trigger_partial_drop':average('green',partial,partial+timedelta(minutes=15))<.5*average('green',partial-timedelta(minutes=15),partial),
        'load_remains_positive_until_last_terminal_drop':all(v>100 for t,v in series['green'] if partial<=t<death),
        'all_traces_zero_from_last_terminal_drop':all(v==0 for points in series.values() for t,v in points if t>=death),
    }
    return expected,metadata,stats,checks

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=['draft','before','write','after'])
    args=parser.parse_args()
    expected,metadata,stats,checks=local_checks()
    if args.phase=='draft':
        result={'passed':all(checks.values()),'checks':checks,'points':len(expected),'stats':stats,'events':metadata['events']}
        (ROOT/'draft-verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result,indent=2));raise SystemExit(0 if result['passed'] else 1)
    if args.phase=='before':
        current=database(); previous=summary(current)
        if 'cassandra_tps' in previous:raise AssertionError('cassandra_tps already exists; inspect before updating it')
        # The alignment is derived from this exact Cassandra history.
        local={('cassandra_response_time',int(datetime.fromisoformat(r['timestamp']).timestamp()),(('host',r['host']),)):
               float(r['cassandra_response_time_ms']) for r in csv.DictReader((DATA.parent/'cassandra-response-time.csv').open(encoding='utf-8'))}
        assert local=={k:v for k,v in current.items() if k[0]=='cassandra_response_time'}
        (ROOT/'before.json').write_text(json.dumps(previous,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(previous,indent=2));return
    if args.phase=='write':
        assert all(checks.values()),checks
        assert (ROOT/'before.json').exists(),'Capture existing data before adding the new measurement'
        body=(DATA/'metrics.lp').read_bytes()
        request(INFLUX+'/api/v2/write?'+urllib.parse.urlencode({'org':ORG,'bucket':BUCKET,'precision':'s'}),body,
                {'Authorization':'Token '+TOKEN,'Content-Type':'text/plain; charset=utf-8'})
        print(f'Wrote {len(expected)} new TPS points');return
    current=database(); previous=json.loads((ROOT/'before.json').read_text(encoding='utf-8'))
    now=summary(current)
    checks['all_existing_series_unchanged']=all(now.get(name)==info for name,info in previous.items())
    checks['new_points_exactly_match_database']={k:v for k,v in current.items() if k[0]=='cassandra_tps'}==expected
    health=json.loads(request('http://localhost:3001/api/datasources/uid/influxdb-replay/health',headers=AUTH))
    checks['grafana_datasource_healthy']=health.get('status')=='OK'
    dashboard=json.loads(request('http://localhost:3001/api/dashboards/uid/cassandra-tps-correlation',headers=AUTH))['dashboard']
    panel=next(p for p in dashboard['panels'] if p['id']==2)
    query=panel['targets'][0]
    payload={'from':str(int(datetime.fromisoformat(dashboard['time']['from'].replace('Z','+00:00')).timestamp()*1000)),
             'to':str(int(datetime.fromisoformat(dashboard['time']['to'].replace('Z','+00:00')).timestamp()*1000)),
             'queries':[{**query,'intervalMs':15000,'maxDataPoints':10000}]}
    answer=json.loads(request('http://localhost:3001/api/ds/query',json.dumps(payload).encode(),{**AUTH,'Content-Type':'application/json'}))['results']['A']
    assert not answer.get('error'),answer.get('error')
    grafana={}
    for frame in answer['frames']:
        fields=frame['schema']['fields']; ti=next(i for i,f in enumerate(fields) if f['type']=='time');vi=next(i for i,f in enumerate(fields) if f['type']=='number')
        trace=fields[vi]['labels']['trace']
        times,values=frame['data']['values'][ti],frame['data']['values'][vi]
        for t,v in zip(times,values):grafana[('cassandra_tps',int(t/1000),(('trace',trace),))]=v
    checks['grafana_raw_points_exactly_match']=grafana==expected
    checks['grafana_no_aggregation']='aggregateWindow' not in query['query']
    result={'passed':all(checks.values()),'checks':checks,'points_added':len(expected),'points_per_trace':metadata['points_per_trace'],
            'measurement':'cassandra_tps','events':metadata['events'],'statistics':stats,
            'before':previous,'after':now,'grafana_frames':len(answer['frames']),
            'grafana_url':'http://localhost:3001/d/cassandra-tps-correlation'}
    (ROOT/'verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2));raise SystemExit(0 if result['passed'] else 1)

if __name__=='__main__':main()
