"""Cross-platform stdlib-only runner. Run from the Atropos root via make."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
COMPOSE = ['docker','compose','-f',str(ROOT/'compose.yaml')]
VM = 'http://127.0.0.1:18428'
CONTROL = 'http://127.0.0.1:18099'
APPS = 'api1:8000|api2:8000|consumer1:8000|consumer2:8000'
CPU = f'rate(process_cpu_seconds_total{{job="experiment-app",instance=~"{APPS}"}}[20s])'
LAG = 'sum(lab_kafka_group_lag)'
QUERIES = {
    'cpu':CPU,
    'container_cpu':'sum by (container_label_com_docker_compose_service) (rate(container_cpu_usage_seconds_total{container_label_com_docker_compose_project="atropos-experiment",image!=""}[20s]))',
    'mean':'sum by (stage,result) (rate(lab_duration_seconds_sum[20s])) / sum by (stage,result) (rate(lab_duration_seconds_count[20s]))',
    'p99':'histogram_quantile(0.99,sum by (le,stage,result) (rate(lab_duration_seconds_bucket[20s])))',
    'stddev':'sqrt(clamp_min(sum by (stage,result) (rate(lab_duration_squared_seconds_total[20s])) / sum by (stage,result) (rate(lab_duration_seconds_count[20s])) - (sum by (stage,result) (rate(lab_duration_seconds_sum[20s])) / sum by (stage,result) (rate(lab_duration_seconds_count[20s])))^2, 0))',
    'throughput':'sum by (stage) (rate(lab_events_total[20s]))',
    'lag':'lab_kafka_group_lag', 'isr':'lab_kafka_isr', 'leader':'lab_kafka_leader',
    'phase':'lab_experiment_phase','disk_active':'lab_disk_stall_active',
    'disk_blocked':'lab_disk_blocked_calls','disk_entries':'lab_disk_gate_entries_total',
    'network_active':'lab_network_fault_active','probe_ok':'lab_kafka_probe_ok',
    'inflight':'lab_requests_inflight',
}


def command(args, **kwargs):
    return subprocess.run(args,check=True,**kwargs)


def api(url, data=None):
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
                                 headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=15) as response:
        return json.load(response)


def query(expr):
    return api(VM+'/api/v1/query?'+urllib.parse.urlencode({'query':expr}))['data']['result']


def scalar(expr, default=0):
    rows = query(expr)
    return float(rows[0]['value'][1]) if rows else default


def wait_ready():
    deadline = time.monotonic()+120
    while time.monotonic() < deadline:
        try:
            for port in (18081,18082):
                api(f'http://127.0.0.1:{port}/health')
            if scalar('sum(up{job="experiment-app"})') == 4 and scalar('sum(lab_assigned_partitions)') == 6:
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('Services/metrics not ready: run docker compose -f infra/experiment/compose.yaml logs')


def reset_faults():
    api(CONTROL+'/disk',{'seconds':0})
    api(CONTROL+'/network',{'seconds':0})
    api(CONTROL+'/phase',{'phase':0})


def drain(timeout=180):
    deadline = time.monotonic()+timeout
    healthy = 0
    while time.monotonic() < deadline:
        ok = (scalar('lab_kafka_probe_ok') == 1 and scalar(LAG, float('inf')) == 0
              and scalar('sum(lab_requests_inflight)', float('inf')) == 0)
        healthy = healthy+1 if ok else 0
        if healthy >= 3:
            return
        time.sleep(3)
    raise RuntimeError('Kafka backlog or active HTTP requests did not drain; refusing a contaminated baseline. Inspect dashboard/logs.')


def start_load(rate, duration, directory):
    directory.mkdir(parents=True,exist_ok=True)
    name = 'atropos-experiment-load'
    subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    log = (directory/'k6.log').open('w',encoding='utf8')
    args = COMPOSE+['run','--rm','--no-deps','--name',name,
                    '-v',str(directory.resolve())+':/results',
                    '-e',f'RATE={rate}','-e',f'DURATION={duration}s',
                    '-e',f'RUN_ID={directory.name}', 'k6','run','/scripts/test.js']
    process = subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT)
    deadline = time.monotonic()+120
    while time.monotonic()<deadline:
        if 'LAB_LOAD_READY' in (directory/'k6.log').read_text(encoding='utf8'):
            return process,log
        if process.poll() is not None:
            log.close()
            raise RuntimeError(f'k6 failed to start: {directory}/k6.log')
        time.sleep(.25)
    stop_load(process,log)
    raise RuntimeError('k6 startup timed out')


def stop_load(process, log):
    if process.poll() is None:
        subprocess.run(['docker','stop','-t','20','atropos-experiment-load'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.terminate()
    log.close()


def metric(summary, name, value, default=0):
    return summary.get('metrics',{}).get(name,{}).get('values',{}).get(value,default)


def calibrate(directory, seconds):
    trials = []
    rate = 40
    best = None
    for index in range(8):
        drain()
        dest = directory/f'calibration-{index}-{rate}'
        process, log = start_load(rate,seconds,dest)
        samples = []
        start = time.monotonic()
        try:
            while process.poll() is None:
                time.sleep(2)
                if 20 < time.monotonic()-start < seconds-1:
                    samples.append({'cpu':{r['metric']['instance']:float(r['value'][1]) for r in query(CPU)},
                                    'lag':scalar(LAG)})
            if process.returncode:
                raise RuntimeError(f'k6 calibration failed; see {dest}/k6.log')
        finally:
            stop_load(process,log)
        summary = json.loads((dest/'summary.json').read_text())
        if not samples:
            raise RuntimeError('Calibration requires at least 25 seconds and fresh CPU samples')
        cpu = {node:sum(s['cpu'].get(node,0) for s in samples)/len(samples) for node in samples[-1]['cpu']}
        hottest = max(cpu.values(),default=0)
        lag_growth = samples[-1]['lag']-samples[0]['lag']
        valid = (metric(summary,'http_req_failed','rate') < .01 and metric(summary,'dropped_iterations','count') == 0
                 and lag_growth <= max(20,rate*.1) and hottest > 0)
        trial = {'rate':rate,'cpu':cpu,'max_cpu':hottest,'lag_growth':lag_growth,'sustainable':valid}
        trials.append(trial)
        print(json.dumps(trial),flush=True)
        if valid and (best is None or abs(hottest-.8)<abs(best['max_cpu']-.8)):
            best = trial
        if valid and .72 <= hottest <= .88:
            break
        if hottest <= 0:
            raise RuntimeError('No CPU metrics; cannot claim calibration')
        factor = min(2.0,max(.5,.8/hottest)) if valid else .65
        next_rate = max(4,int(rate*factor)//2*2)
        if next_rate == rate:
            break
        rate = next_rate
    if best is None:
        raise RuntimeError('No sustainable load found')
    result = {'selected':best,'trials':trials,'target':.8,'created_at':time.time()}
    (directory/'calibration.json').write_text(json.dumps(result,indent=2))
    (ROOT/'.state').mkdir(exist_ok=True)
    (ROOT/'.state'/'calibration.json').write_text(json.dumps(result,indent=2))
    if not .7 <= best['max_cpu'] <= .9:
        raise RuntimeError('No stable 70-90% CPU point. See calibration.json; adjust CPU_ITERATIONS and redeploy, or explicitly pass --rate.')
    drain()
    return best['rate']


def export(directory,start,end):
    for name,expr in QUERIES.items():
        try:
            data = api(VM+'/api/v1/query_range?'+urllib.parse.urlencode({'query':expr,'start':start,'end':end,'step':2}))
            (directory/f'{name}.json').write_text(json.dumps(data))
        except Exception as exc:
            print(f'Export {name} failed: {exc}',file=sys.stderr)
    result = subprocess.run(COMPOSE+['logs','--no-color','--since',dt.datetime.fromtimestamp(start,dt.timezone.utc).isoformat()],capture_output=True)
    (directory/'containers.log').write_bytes(result.stdout+result.stderr)
    result = subprocess.run(COMPOSE+['exec','-T','observer','cat','/faults/disk-events.log'],capture_output=True)
    (directory/'disk-events.log').write_bytes(result.stdout)


def run_test(args):
    wait_ready()
    reset_faults()
    drain()
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = ROOT/'results'/f'{stamp}-{args.action}'
    directory.mkdir(parents=True)
    config = command(COMPOSE+['config'],capture_output=True)
    (directory/'compose-resolved.yaml').write_bytes(config.stdout)
    images = command(COMPOSE+['images','--format','json'],capture_output=True)
    (directory/'images.json').write_bytes(images.stdout)
    if args.rate:
        rate = args.rate
    else:
        rate = calibrate(directory,args.calibration_seconds)
    # Prefer original replica leaders before each experiment, not a different topology left by failover.
    command(COMPOSE+['exec','-T','kafka2','/opt/kafka/bin/kafka-leader-election.sh','--bootstrap-server','kafka2:9092',
                     '--election-type','preferred','--all-topic-partitions'],stdout=subprocess.DEVNULL)
    duration = args.baseline+args.fault+args.recovery
    manifest = {'action':args.action,'rate':rate,'baseline_s':args.baseline,'fault_s':args.fault,
                'recovery_s':args.recovery,'latency_ms_per_direction':args.latency_ms,'jitter_ms':args.jitter_ms,
                'cpu_iterations':os.environ.get('CPU_ITERATIONS','12000'),'start':time.time(),'events':[]}
    start = manifest['start']
    blocked_before = scalar('lab_disk_gate_entries_total')
    process,log = start_load(rate,duration,directory)
    try:
        for phase,seconds in [(1,args.baseline),(2,args.fault),(3,args.recovery)]:
            if phase == 2:
                if args.action == 'test1':
                    api(CONTROL+'/disk',{'seconds':args.fault})
                else:
                    api(CONTROL+'/network',{'seconds':args.fault,'latency_ms':args.latency_ms,'jitter_ms':args.jitter_ms})
            if phase == 3:
                reset_faults()
            api(CONTROL+'/phase',{'phase':phase,'ttl':seconds+30,'rate':rate})
            manifest['events'].append({'phase':phase,'timestamp':time.time()})
            print(f'Phase {phase}, {seconds}s, fixed {rate} requests/s. Grafana: http://localhost:13001/d/experiment',flush=True)
            deadline = time.monotonic()+seconds
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    if phase == 3 and deadline-time.monotonic()<5:
                        break
                    raise RuntimeError('k6 exited before phase completion; inspect k6.log')
                time.sleep(min(2,max(0,deadline-time.monotonic())))
        code = process.wait(timeout=40)
        if code:
            raise RuntimeError(f'k6 exit {code}; dropped iterations invalidate fixed offered load')
        if args.action == 'test1' and scalar('lab_disk_gate_entries_total') <= blocked_before:
            raise RuntimeError('No actual disk syscalls intercepted: incident NOT validated')
        summary = json.loads((directory/'summary.json').read_text())
        manifest['dropped_iterations'] = metric(summary,'dropped_iterations','count')
        if manifest['dropped_iterations']:
            raise RuntimeError('Load generator dropped iterations')
        drain()
        manifest['status'] = 'completed'
    except BaseException as exc:
        manifest['status'] = 'failed'
        manifest['error'] = str(exc)
        raise
    finally:
        cleanup_error = None
        try:
            reset_faults()
        except Exception as exc:
            cleanup_error = exc
            manifest['cleanup_error'] = str(exc)
            manifest['status'] = 'cleanup_failed'
        finally:
            stop_load(process,log)
        manifest['end'] = time.time()
        (directory/'manifest.json').write_text(json.dumps(manifest,indent=2))
        export(directory,start,manifest['end'])
        print(f'Results: {directory}',flush=True)
        if cleanup_error:
            raise RuntimeError('Fault cleanup failed; TTL remains the fallback; inspect manifest') from cleanup_error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['up','test1','test2','down'])
    parser.add_argument('--rate',type=int)
    parser.add_argument('--baseline',type=int,default=60)
    parser.add_argument('--fault',type=int,default=60)
    parser.add_argument('--recovery',type=int,default=90)
    parser.add_argument('--calibration-seconds',type=int,default=40)
    parser.add_argument('--latency-ms',type=int,default=100)
    parser.add_argument('--jitter-ms',type=int,default=30)
    args = parser.parse_args()
    if args.rate is not None and args.rate<4:
        parser.error('--rate must be >= 4')
    if min(args.baseline,args.fault,args.recovery)<=0 or args.fault>600:
        parser.error('phases must be positive; fault must be <= 600s')
    if args.latency_ms<0 or not 0<=args.jitter_ms<=args.latency_ms:
        parser.error('require 0 <= jitter <= latency')
    if args.calibration_seconds<25:
        parser.error('calibration requires >= 25s')
    if args.action == 'up':
        command(COMPOSE+['--profile','load','pull','k6'])
        command(COMPOSE+['build','kafka1','api1','observer'])
        command(COMPOSE+['up','-d','--wait','--wait-timeout','300'])
        wait_ready()
        reset_faults()
        print('Ready. Grafana http://localhost:13001/d/experiment ; metrics http://localhost:18428')
    elif args.action == 'down':
        try:
            reset_faults()
        except Exception:
            pass
        subprocess.run(['docker','rm','-f','atropos-experiment-load'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        command(COMPOSE+['down','--remove-orphans'])
    else:
        # Refuse concurrent tests, and retain a helpful lock instead of racing fault schedules.
        state = ROOT/'.state'
        state.mkdir(exist_ok=True)
        lock = state/'test.lock'
        try:
            fd = os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        except FileExistsError:
            raise RuntimeError(f'Another test owns {lock}; after a crashed runner, check it before removing')
        try:
            os.write(fd,str(os.getpid()).encode())
            os.close(fd)
            run_test(args)
        finally:
            lock.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        sys.exit(1)
