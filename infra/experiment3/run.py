"""Run and verify a bounded Docker OOM experiment. Standard library only."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import shutil
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
COMPOSE = ['docker', 'compose', '-f', str(ROOT / 'compose.yaml')]
VM = 'http://localhost:8428'
BASELINE = int(os.getenv('BASELINE_SECONDS', '300'))
RPS = int(os.getenv('LOAD_RPS', '750'))

def command(*args, **kwargs):
    return subprocess.run([*COMPOSE, *args], check=True, **kwargs)

def get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode()

def inspect(service):
    cid = command('ps', '-a', '-q', service, capture_output=True, text=True).stdout.strip()
    return json.loads(subprocess.check_output(['docker', 'inspect', cid], text=True))[0]

def up():
    command('up', '-d', '--build', '--force-recreate', '--wait', 'service1', 'service2')

def run():
    # Only these experiment containers are replaced. Monitoring and its data are preserved.
    command('--profile', 'load', 'rm', '-s', '-f', 'k6', 'service1', 'service2')
    results = ROOT / 'results'
    results.mkdir(exist_ok=True)
    gc_log = results / 'gc' / 'service1-gc.log'
    gc_log.parent.mkdir(exist_ok=True)
    if gc_log.exists(): gc_log.unlink()
    summary = results / 'k6-summary.json'
    if summary.exists(): summary.unlink()
    up()
    # Capture full lifecycle state, and sample /metrics even if VictoriaMetrics is unavailable.
    start = time.time()
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = results / stamp
    out.mkdir()
    command('--profile', 'load', 'up', '-d', 'k6')
    samples = out / 'metrics.ndjson'
    oom_time = None
    killed = None
    try:
        while time.time() - start < 625:
            state = inspect('service1')
            record = {'time': time.time(), 'state': state['State']}
            for port, service in [(18281, 'service1'), (18282, 'service2')]:
                try: record[service] = get(f'http://localhost:{port}/metrics')
                except Exception as e: record[service] = str(e)
            with samples.open('a', encoding='utf-8') as f: f.write(json.dumps(record) + '\n')
            jvm_oom = False
            if not state['State']['Running']:
                log_text = subprocess.run([*COMPOSE,'logs','--no-color','service1'],capture_output=True,text=True).stdout
                jvm_oom = 'java.lang.OutOfMemoryError' in log_text and state['State']['ExitCode'] != 0
            if (state['State']['OOMKilled'] or jvm_oom) and oom_time is None:
                killed = state
                finished = state['State']['FinishedAt']
                # Docker nanosecond precision exceeds datetime: fromisoformat accepts/truncates it.
                oom_time = dt.datetime.fromisoformat(finished.replace('Z', '+00:00')).timestamp()
                print(f'Confirmed service1 OOM after {oom_time - start:.1f}s (cgroup={state["State"]["OOMKilled"]}, JVM={jvm_oom})', flush=True)
                # The experiment is complete at OOM. Do not keep a load generator
                # polling an exited container for the rest of the ten-minute deadline.
                command('--profile','load','stop','k6')
                time.sleep(3)
                break
            if not inspect('k6')['State']['Running']: break
            time.sleep(5)
        else: raise RuntimeError('k6 did not finish within 625 seconds')
    finally:
        (out / 'docker-state.json').write_text(json.dumps({s: inspect(s) for s in ['service1', 'service2', 'k6']}, indent=2), encoding='utf-8')
        for s in ['service1', 'service2', 'k6']:
            p = subprocess.run([*COMPOSE, 'logs', '--no-color', s], capture_output=True, text=True)
            (out / f'{s}.log').write_text(p.stdout + p.stderr, encoding='utf-8')
        # Ensure a failed or interrupted verification cannot leave background load running.
        command('--profile', 'load', 'stop', 'k6')
        if gc_log.exists(): shutil.copy2(gc_log,out / 'service1-gc.log')
    k6 = json.loads(summary.read_text()) if summary.exists() else {}
    (out / 'k6-summary.json').write_text(json.dumps(k6, indent=2), encoding='utf-8')
    metrics = k6.get('metrics', {})
    dropped = metrics.get('dropped_iterations', {}).get('count', 0)
    requests = metrics.get('http_reqs', {}).get('count', 0)
    s2 = inspect('service2')['State']
    records = [json.loads(line) for line in samples.read_text(encoding='utf-8').splitlines()]
    def values(service, name):
        vals = []
        for r in records:
            for line in r.get(service, '').splitlines():
                if line.startswith(name + '{'): vals.append(float(line.rsplit(' ', 1)[1]))
        return vals
    # Measure steady baseline after JVM warmup, using counter DELTAS rather than startup totals.
    baseline_records = [r for r in records if 60 < r['time'] - start < BASELINE-10]
    def counter_delta(name):
        values = [float(line.rsplit(' ',1)[1]) for r in baseline_records
            for line in r.get('service1','').splitlines() if line.startswith(name+'{')]
        return values[-1]-values[0] if len(values)>1 else None
    baseline_timeout = counter_delta('exp3_timeouts_total')
    baseline_received = counter_delta('exp3_received_total')
    checks = {
        'service1_confirmed_oom': bool(killed),
        'oom_after_baseline_before_600s': oom_time is not None and BASELINE < oom_time - start <= 600,
        'service2_survived': s2['Running'] and not s2['OOMKilled'],
        'k6_no_dropped_iterations': dropped == 0,
        'k6_steady_offered_rate': metrics.get('http_reqs',{}).get('rate',0) >= RPS*0.98,
        'baseline_timeout_fraction_below_1pct': baseline_timeout is not None and baseline_received is not None and baseline_timeout / max(baseline_received,1) < 0.01,
        'stable_queue_after_warmup': max([float(line.rsplit(' ',1)[1]) for r in baseline_records
            for line in r.get('service1','').splitlines() if line.startswith('exp3_queue_depth{')] or [1e9]) < 500,
        'queue_grew': max(values('service1','exp3_queue_depth') or [0]) > 10000,
        'retries_occurred': max(values('service1','exp3_retries_total') or [0]) > 1000,
        'degraded_phase_observed': max(values('service2','exp3_degraded') or [0]) == 1,
        'minor_gc_observed': any('Pause Young' in line for line in gc_log.read_text().splitlines()) if gc_log.exists() else False,
        'major_gc_observed': any('Pause Full' in line for line in gc_log.read_text().splitlines()) if gc_log.exists() else False,
    }
    end = time.time()
    verification = {'started_at': start, 'finished_at': end, 'oom_after_seconds': None if oom_time is None else oom_time-start,
        'oom_kind': None if killed is None else ('cgroup' if killed['State']['OOMKilled'] else 'JVM heap'),
        'checks': checks, 'baseline_timeouts': baseline_timeout, 'baseline_received': baseline_received,
        'max_queue': max(values('service1','exp3_queue_depth') or [0]),
        'grafana_url': f'http://localhost:3001/d/experiment3-retry-oom?from={int(start*1000)}&to={int(end*1000)}'}
    for p in panels_for_export():
        query = urllib.parse.urlencode({'query': p, 'start': start, 'end': end, 'step': '2s'})
        try:
            data = get(f'{VM}/api/v1/query_range?{query}')
            with (out / 'victoria-series.ndjson').open('a', encoding='utf-8') as f:
                f.write(json.dumps({'query': p, 'response': json.loads(data)})+'\n')
        except Exception as e: verification.setdefault('export_errors', []).append(str(e))
    (out / 'verification.json').write_text(json.dumps(verification, indent=2), encoding='utf-8')
    print(json.dumps(verification, indent=2), flush=True)
    if not all(checks.values()): raise SystemExit('Experiment verification failed; see '+str(out))

def panels_for_export():
    data = json.loads((ROOT / 'dashboard.json').read_text(encoding='utf-8'))
    return list(dict.fromkeys(t['expr'] for p in data['panels'] for t in p['targets']))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['up', 'run', 'down'])
    args = parser.parse_args()
    if args.action == 'up': up()
    elif args.action == 'run': run()
    else: command('--profile', 'load', 'down')
