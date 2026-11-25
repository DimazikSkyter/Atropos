"""Validate the two new historical measurements and preserve all prior replay data."""
import argparse,base64,csv,hashlib,io,json,math,os,statistics
from pathlib import Path
from datetime import datetime,timedelta
import urllib.request,urllib.parse

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'infra'/'influxdb'/'sync-dispatcher' if (ROOT/'infra'/'influxdb'/'sync-dispatcher').exists() else ROOT
CLIENT='sync_service_client_response_time';DISPATCHER='service_dispatcher_delay';NEW={CLIENT,DISPATCHER}
INFLUX=os.getenv('INFLUX_URL','http://localhost:8086').rstrip('/')
ORG=os.getenv('INFLUXDB_ORG','atropos');BUCKET=os.getenv('INFLUXDB_BUCKET','metrics-replay')
TOKEN=os.getenv('INFLUXDB_TOKEN','atropos-local-replay-token')
AUTH={'Authorization':'Basic '+base64.b64encode((os.getenv('GRAFANA_ADMIN_USER','admin')+':'+os.getenv('GRAFANA_ADMIN_PASSWORD','admin')).encode()).decode()}

def request(url,data=None,headers=None):
    req=urllib.request.Request(url,data=data,headers=headers or {})
    with urllib.request.urlopen(req,timeout=30) as response:return response.read().decode()

def database():
    flux=f'from(bucket: "{BUCKET}") |> range(start: 0) |> filter(fn: (r) => r._field == "value")'
    raw=request(INFLUX+'/api/v2/query?'+urllib.parse.urlencode({'org':ORG}),flux.encode(),
                {'Authorization':'Token '+TOKEN,'Content-Type':'application/vnd.flux','Accept':'application/csv'})
    points={};header=None
    for fields in csv.reader(io.StringIO(raw)):
        if not fields or fields[0].startswith('#'):continue
        if '_time' in fields:header=fields;continue
        row=dict(zip(header,fields))
        if not row.get('_time'):continue
        tags=tuple(sorted((k,v) for k,v in row.items() if k not in
                          {'','result','table','_start','_stop','_time','_value','_field','_measurement'} and v))
        identity=(row['_measurement'],int(datetime.fromisoformat(row['_time'].replace('Z','+00:00')).timestamp()),tags)
        assert identity not in points,'Duplicate returned identity'
        points[identity]=float(row['_value'])
    return points

def summary(points):
    result={}
    for metric in sorted({key[0] for key in points}):
        part=sorted((key,value) for key,value in points.items() if key[0]==metric)
        result[metric]={'points':len(part),'sha256':hashlib.sha256(json.dumps(part,separators=(',',':')).encode()).hexdigest()}
    return result

def link_window(url):
    params=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    start,end=params.get('from',[''])[0],params.get('to',[''])[0]
    assert len(start)==len(end)==13 and start.isdigit() and end.isdigit(),f'Expected Unix milliseconds in URL: {url}'
    assert int(start)<int(end),f'Invalid URL time range: {url}'
    return {'from':start,'to':end}

def local():
    meta=json.loads((DATA/'metadata.json').read_text(encoding='utf-8'))
    expected={};series={}
    for r in csv.DictReader((DATA/'metrics.csv').open(encoding='utf-8')):
        t=datetime.fromisoformat(r['timestamp']);v=float(r['value']);key=(r['measurement'],r['host'])
        expected[(r['measurement'],int(t.timestamp()),(('host',r['host']),))]=v
        series.setdefault(key,[]).append((t,v))
    onset=datetime.fromisoformat(meta['level_jump_time'])
    first_growth=min(t for (metric,_),points in series.items() if metric==DISPATCHER for t,v in points if v>.020)
    baseline_start=datetime.fromisoformat(meta['dispatcher_baseline']['start'])
    dispatcher_baseline=[v for (metric,_),points in series.items() if metric==DISPATCHER for t,v in points if baseline_start<=t<=onset]
    baseline=[v for (metric,_),points in series.items() if metric==CLIENT for t,v in points if t<onset-timedelta(seconds=15)]
    plateau=[v for (metric,_),points in series.items() if metric==CLIENT for t,v in points if '18:22:15'<=t.strftime('%H:%M:%S')<='18:23:00']
    peaks={host:max(v for _,v in points) for (metric,host),points in series.items() if metric==DISPATCHER}
    checks={
        'new_measurements_present':{k[0] for k in series}==NEW,
        'point_count_matches':len(expected)==meta['total_points']==3073,
        'all_points_on_requested_date':all(t.date().isoformat()=='2026-10-05' and t.utcoffset()==timedelta(hours=3) for points in series.values() for t,_ in points),
        'all_values_finite_nonnegative':all(math.isfinite(v) and v>=0 for v in expected.values()),
        'uniform_five_second_step':all((b[0]-a[0]).total_seconds()==5 for points in series.values() for a,b in zip(points,points[1:])),
        'eight_client_hosts':len([1 for metric,_ in series if metric==CLIENT])==8,
        'five_visible_dispatcher_hosts':len([1 for metric,_ in series if metric==DISPATCHER])==5,
        'only_main_client_band':all(5<=v<=25 for (metric,_),points in series.items() if metric==CLIENT for _,v in points),
        'client_baseline_about_6ms':5.5<=statistics.median(baseline)<=6.5,
        'client_plateau_about_24ms':23<=statistics.median(plateau)<=24.5,
        'dispatcher_growth_aligned_to_level_jump':0<=(first_growth-onset).total_seconds()<=15,
        'dispatcher_baseline_10_to_20ms':len(dispatcher_baseline)==260 and all(.010<=v<=.020 for v in dispatcher_baseline),
        'no_dispatcher_growth_before_jump':all(v<=.020 for (metric,_),points in series.items() if metric==DISPATCHER for t,v in points if t<=onset),
        'dispatcher_main_peak_about_8s':7.5<=max(peaks.values())<=8.0,
        'later_dispatcher_peaks_preserved':max(v for (metric,_),points in series.items() if metric==DISPATCHER for t,v in points if t.strftime('%H:%M:%S')>='18:35:00')>=3.5,
        'client_source_range_only':all('18:15:00'<=t.strftime('%H:%M:%S')<='18:25:00' for (metric,_),points in series.items() if metric==CLIENT for t,_ in points),
        'correct_units':meta['units']=={CLIENT:'ms',DISPATCHER:'s'},
    }
    stats={'first_level_jump':onset.isoformat(),'first_dispatcher_growth':first_growth.isoformat(),
           'dispatcher_baseline_min_ms':min(dispatcher_baseline)*1000,'dispatcher_baseline_max_ms':max(dispatcher_baseline)*1000,
           'onset_difference_seconds':(first_growth-onset).total_seconds(),'client_baseline_median_ms':statistics.median(baseline),
           'client_plateau_median_ms':statistics.median(plateau),'dispatcher_peaks_seconds':peaks,'points_per_series':meta['points_per_series']}
    return expected,meta,checks,stats

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('phase',choices=['draft','before','write','after']);args=parser.parse_args()
    expected,meta,checks,stats=local()
    if args.phase=='draft':
        result={'passed':all(checks.values()),'checks':checks,'statistics':stats}
        (ROOT/'draft-verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result,indent=2));raise SystemExit(0 if result['passed'] else 1)
    if args.phase=='before':
        before=summary(database());assert not NEW.intersection(before),'New names already exist; inspect before replacing'
        (ROOT/'before.json').write_text(json.dumps(before,indent=2)+'\n',encoding='utf-8');print(json.dumps(before,indent=2));return
    if args.phase=='write':
        assert all(checks.values()),checks
        assert (ROOT/'before.json').exists(),'Capture baseline first'
        request(INFLUX+'/api/v2/write?'+urllib.parse.urlencode({'org':ORG,'bucket':BUCKET,'precision':'s'}),
                (DATA/'metrics.lp').read_bytes(),{'Authorization':'Token '+TOKEN,'Content-Type':'text/plain; charset=utf-8'})
        print(f'Wrote {len(expected)} points in two measurements');return
    current=database();before=json.loads((ROOT/'before.json').read_text(encoding='utf-8'));after=summary(current)
    checks['all_previous_measurements_unchanged']=all(after.get(name)==details for name,details in before.items())
    checks['database_points_exact_match']={k:v for k,v in current.items() if k[0] in NEW}==expected
    dashboard=json.loads(request('http://localhost:3001/api/dashboards/uid/sync-client-dispatcher-replay',headers=AUTH))['dashboard']
    windows=[link_window(link['url']) for link in dashboard['links']]
    expected_windows=[{'from':str(int(datetime.fromisoformat(start).timestamp()*1000)),
                       'to':str(int(datetime.fromisoformat(end).timestamp()*1000))}
                      for start,end in [('2026-10-05T18:18:30+03:00','2026-10-05T18:25:30+03:00'),
                                        ('2026-10-05T18:10:00+03:00','2026-10-05T18:45:00+03:00')]]
    checks['dashboard_links_have_correct_epoch_windows']=windows==expected_windows
    fetched={};frames={}
    for panel in [p for p in dashboard['panels'] if p['type']=='timeseries']:
        metric=CLIENT if panel['id']==1 else DISPATCHER
        target=panel['targets'][0]
        payload={**windows[1],
                 'queries':[{**target,'intervalMs':5000,'maxDataPoints':10000}]}
        answer=json.loads(request('http://localhost:3001/api/ds/query',json.dumps(payload).encode(),{**AUTH,'Content-Type':'application/json'}))['results']['A']
        assert not answer.get('error'),answer.get('error')
        for frame in answer['frames']:
            fields=frame['schema']['fields'];ti=next(i for i,f in enumerate(fields) if f['type']=='time');vi=next(i for i,f in enumerate(fields) if f['type']=='number')
            host=fields[vi]['labels']['host'];times,values=frame['data']['values'][ti],frame['data']['values'][vi]
            for t,v in zip(times,values):fetched[(metric,int(t/1000),(('host',host),))]=v
        frames[metric]=len(answer['frames'])
        checks['grafana_unit_'+metric]=panel['fieldConfig']['defaults']['unit']==meta['units'][metric]
        checks['grafana_legend_'+metric]=('ВО клиента' if metric==CLIENT else 'Диспетчер') in panel['fieldConfig']['defaults']['displayName']
        checks['grafana_no_aggregation_'+metric]='aggregateWindow' not in target['query']
        focus_payload={**payload,**windows[0]}
        focus_answer=json.loads(request('http://localhost:3001/api/ds/query',json.dumps(focus_payload).encode(),{**AUTH,'Content-Type':'application/json'}))['results']['A']
        checks['focus_link_returns_data_'+metric]=not focus_answer.get('error') and len(focus_answer.get('frames',[]))==frames[metric]
    checks['grafana_raw_points_exact_match']=fetched==expected
    checks['grafana_correct_host_frames']=frames=={CLIENT:8,DISPATCHER:5}
    health=json.loads(request('http://localhost:3001/api/datasources/uid/influxdb-replay/health',headers=AUTH))
    checks['grafana_datasource_healthy']=health.get('status')=='OK'
    overview=json.loads(request('http://localhost:3001/api/dashboards/uid/influxdb-replay',headers=AUTH))['dashboard']
    checks['overview_contains_new_replay']=any('/d/sync-client-dispatcher-replay' in link.get('url','') for link in overview['links'])
    overview_links=overview['links']+[link for panel in overview['panels'] for link in panel.get('links',[])]
    checks['overview_links_use_epoch_windows']=all(link_window(link['url']) for link in overview_links)
    result={'passed':all(checks.values()),'checks':checks,'statistics':stats,'points_added':len(expected),
            'before':before,'after':after,'grafana_frames':frames,'grafana_url':'http://localhost:3001/d/sync-client-dispatcher-replay'}
    (ROOT/'verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2));raise SystemExit(0 if result['passed'] else 1)

if __name__=='__main__':main()
