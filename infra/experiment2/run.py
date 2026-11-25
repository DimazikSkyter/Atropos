"""Run from any directory. Reuses the existing monitoring stack, never creates or stops it."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import urllib.request
from metrics import QUERIES
from dashboard import build

ROOT=Path(__file__).resolve().parent
VM=os.environ.get('EXPERIMENT2_VM_URL','http://127.0.0.1:8428')
COMPOSE=['docker','compose','-f',str(ROOT/'compose.yaml')]
PORTS=range(18181,18185)

def command(args,**kw):
    return subprocess.run(args,check=True,**kw)

def fetch(url):
    with urllib.request.urlopen(url,timeout=15) as r: return json.load(r)

def save(path,value): path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

def query(expr):
    data=fetch(VM+'/api/v1/query?'+urllib.parse.urlencode({'query':expr}))
    if data['status']!='success': raise RuntimeError(data)
    return data['data']['result']

def metrics():
    result=[]
    for port in PORTS:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics',timeout=10) as r:
            body=r.read().decode()
        result.append(body)
    return result

def values(bodies,name):
    found=[]
    for body in bodies:
        for line in body.splitlines():
            if re.match(re.escape(name)+r'(\{| )',line): found.append(float(line.split()[-1]))
    return found

def wait_ready():
    until=time.monotonic()+180
    while time.monotonic()<until:
        try:
            health=[fetch(f'http://127.0.0.1:{p}/health') for p in PORTS]
            assignments=[h['partitions'] for h in health[2:]]
            if all(h['ready'] for h in health) and sorted(assignments)==[[0],[1]]:
                return health
        except Exception: pass
        time.sleep(2)
    raise RuntimeError('Services did not become ready with one partition each')

def drain(timeout=120):
    deadline=time.monotonic()+timeout; stable=0
    while time.monotonic()<deadline:
        bodies=metrics()
        lag=values(bodies,'exp2_kafka_lag'); probe=values(bodies,'exp2_kafka_probe_ok')
        ok=len(lag)==4 and all(v==0 for v in lag) and probe==[1,1]
        stable=stable+1 if ok else 0
        if stable>=3: return
        time.sleep(2)
    raise RuntimeError('Kafka queue did not drain; inspect service errors and lag')

def monitor():
    # Modify only our job and dashboard. Existing data, jobs and datasource remain intact.
    infra=ROOT.parent
    scrape=infra/'victoria-metrics'/'scrape.yml'
    text=scrape.read_text(encoding='utf-8-sig')
    if 'job_name: experiment2' not in text:
        text+='\n  - job_name: experiment2\n    scrape_interval: 2s\n    static_configs:\n      - targets: [host.docker.internal:18181, host.docker.internal:18182, host.docker.internal:18183, host.docker.internal:18184]\n'
        scrape.write_text(text,encoding='utf-8')
        command(['docker','restart','monitoring-victoria-metrics-1'])
    destination=infra/'grafana'/'dashboards'/'experiment2-cache.json'
    save(destination,build())
    save(ROOT/'dashboard.json',build())
    # Docker Desktop bind mounts may not deliver filesystem notifications to Grafana.
    # Re-read provisioning explicitly; don't restart Grafana or expose credentials.
    config=json.loads(subprocess.check_output(['docker','inspect','monitoring-grafana-1']))[0]
    env=dict(item.split('=',1) for item in config['Config']['Env'])
    auth=base64.b64encode((env.get('GF_SECURITY_ADMIN_USER','admin')+':'+env.get('GF_SECURITY_ADMIN_PASSWORD','admin')).encode()).decode()
    request=urllib.request.Request('http://127.0.0.1:3001/api/admin/provisioning/dashboards/reload',data=b'',headers={'Authorization':'Basic '+auth},method='POST')
    with urllib.request.urlopen(request,timeout=30) as response:
        if response.status!=200: raise RuntimeError('Grafana provisioning reload failed')
    request=urllib.request.Request('http://127.0.0.1:3001/api/dashboards/uid/experiment2-cache',headers={'Authorization':'Basic '+auth})
    with urllib.request.urlopen(request,timeout=15) as response:
        if len(json.load(response)['dashboard']['panels'])!=14: raise RuntimeError('Dashboard verification failed')

def check_scrape():
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        try:
            rows=query('up{job="experiment2"}')
            if len(rows)==4 and all(float(r['value'][1])==1 for r in rows): return
        except Exception: pass
        time.sleep(2)
    raise RuntimeError('Existing VictoriaMetrics cannot scrape all four experiment2 targets')

def run(args):
    wait_ready(); drain()
    # Fresh JVMs clear local caches, counters and JIT state consistently between repetitions.
    command(COMPOSE+['restart','api1','api2','consumer1','consumer2'])
    health=wait_ready(); check_scrape()
    run_id=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory=ROOT/'results'/run_id; directory.mkdir(parents=True)
    manifest={'id':run_id,'status':'running','rate':args.rate,'seed':args.seed,'warm':args.warm,
              'baseline':args.baseline,'diverse':args.diverse,'recovery':args.recovery,'start':time.time(),'health':health}
    manifest['source_sha256']={str(path.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in [ROOT/'load/test.js',ROOT/'app/src/main/java/lab/Main.java',ROOT/'app/pom.xml',ROOT/'run.py',ROOT/'metrics.py']}
    save(directory/'manifest.json',manifest)
    for filename,cmd in [('compose-resolved.yaml',COMPOSE+['config']),('images.json',COMPOSE+['images','--format','json'])]:
        with (directory/filename).open('w',encoding='utf-8') as out: command(cmd,stdout=out)
    k6=COMPOSE+['run','--rm','--name','atropos-experiment2-load','-v',f'{directory.as_posix()}:/results']
    for key in ('rate','seed','warm','baseline','diverse','recovery'): k6+=['-e',f'{key.upper()}={getattr(args,key)}']
    k6+=['k6']
    print('Results:',directory,flush=True)
    try:
        with (directory/'k6.log').open('w',encoding='utf-8') as out:
            process=subprocess.Popen(k6,stdout=out,stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    time.sleep(10)
                    print('Load running, elapsed',round(time.time()-manifest['start']),'s',flush=True)
            except BaseException:
                subprocess.run(['docker','stop','-t','5','atropos-experiment2-load'],check=False)
                process.wait(timeout=30)
                raise
        if process.returncode: raise RuntimeError(f'k6 failed ({process.returncode}); see k6.log')
        drain(); time.sleep(5)
        bodies=metrics()
        for port,body in zip(PORTS,bodies): (directory/f'metrics-{port}.txt').write_text(body,encoding='utf-8')
        produced=sum(values(bodies,'exp2_produced_total')); consumed=sum(values(bodies,'exp2_consumed_total'))
        manifest.update(produced=produced,consumed=consumed)
        summary=json.loads((directory/'summary.json').read_text())['metrics']
        dropped=summary.get('dropped_iterations',{}).get('values',{}).get('count',0)
        failed=summary['http_req_failed']['values']['rate']
        if produced!=consumed or produced!=summary['http_reqs']['values']['count'] or dropped or failed:
            raise RuntimeError(f'Count/error mismatch: produced={produced}, consumed={consumed}, dropped={dropped}, failed={failed}')
        manifest['status']='completed'
    except BaseException as e:
        manifest.update(status='failed',error=str(e))
        raise
    finally:
        manifest['end']=time.time()
        save(directory/'manifest.json',manifest)
        export_errors=[]
        save(directory/'queries.json',QUERIES)
        for key,expr in QUERIES.items():
            try:
                data=fetch(VM+'/api/v1/query_range?'+urllib.parse.urlencode({'query':expr,'start':manifest['start'],'end':manifest['end'],'step':2,'nocache':1,'latency_offset':'1ms'}))
                if data.get('status')!='success' or not data.get('data',{}).get('result'):
                    raise RuntimeError('Empty or unsuccessful query result')
                save(directory/(key+'.json'),data)
            except Exception as e:
                export_errors.append(key); print('Export failed',key,e,flush=True)
        manifest['export_errors']=export_errors
        if export_errors and manifest['status']=='completed': manifest['status']='export_failed'
        save(directory/'manifest.json',manifest)
        with (directory/'containers.log').open('w',encoding='utf-8') as out:
            subprocess.run(COMPOSE+['logs','--no-color','--since',str(int(manifest['start']))],stdout=out,stderr=subprocess.STDOUT)
    if manifest['status']!='completed': raise RuntimeError('Incomplete metrics export: '+str(manifest['export_errors']))
    print('Completed:',directory,flush=True)
    print(f'Grafana: http://localhost:3001/d/experiment2-cache?from={int(manifest["start"]*1000)}&to={int(manifest["end"]*1000)}',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['up','run','down'])
    for name,default in [('rate',200),('seed',20260930),('warm',60),('baseline',90),('diverse',120),('recovery',90)]: p.add_argument('--'+name,type=int,default=default)
    args=p.parse_args()
    if any(getattr(args,k)<=0 for k in ('rate','warm','baseline','diverse','recovery')): p.error('rate and durations must be positive')
    if args.action=='down': command(COMPOSE+['down'])
    elif args.action=='up':
        command(COMPOSE+['build','api1']); command(COMPOSE+['up','-d','--no-build']); wait_ready(); monitor(); check_scrape()
        print('Ready. Grafana: http://localhost:3001/d/experiment2-cache')
    else:
        lock=ROOT/'.running'
        try: fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        except FileExistsError: raise SystemExit('Another run is active (.running exists)')
        try:
            os.close(fd); run(args)
        finally: lock.unlink(missing_ok=True)
