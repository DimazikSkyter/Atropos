"""Run experiment4 using the existing monitoring stack; Python standard library only."""
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
VM = os.getenv('VICTORIA_METRICS_URL', 'http://localhost:8428').rstrip('/')
SERVICE = os.getenv('EXPERIMENT4_URL', 'http://localhost:18381').rstrip('/')


def http(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode()


def compose(env, *args, check=True):
    result = subprocess.run(['docker', 'compose', '-f', str(ROOT / 'compose.yaml'), *args],
                            env=env, check=False, text=True, encoding='utf-8', errors='replace',
                            capture_output=True)
    if check and result.returncode:
        show(result)
        result.check_returncode()
    return result


def show(result):
    if result.stdout.strip():
        print(result.stdout.strip(), flush=True)
    if result.stderr.strip():
        print(result.stderr.strip(), flush=True)


def inspect(env, service):
    cid = compose(env, '--profile', 'load', 'ps', '-a', '-q', service).stdout.strip()
    if not cid:
        return None
    return json.loads(subprocess.check_output(['docker', 'inspect', cid], env=env))[0]


def up(env):
    show(compose(env, 'up', '-d', '--build', '--force-recreate', '--wait', 'java'))


def run(args):
    env = os.environ.copy()
    if args.pilot:
        env.update(BASELINE_SECONDS='20', RAMP_SECONDS='90', HOLD_SECONDS='30')
    if args.control:
        env['ALLOC_END_MIB'] = env.get('ALLOC_START_MIB', '16')
    duration = sum(int(env.get(k, str(v))) for k, v in
                   [('BASELINE_SECONDS', 180), ('RAMP_SECONDS', 900), ('HOLD_SECONDS', 180)])
    if duration < 900 and not args.pilot:
        raise ValueError('Use >=900 seconds for a full experiment; --pilot marks calibration explicitly')
    # Verify a reachable shared datasource before consuming a full experiment.
    json.loads(http(VM + '/api/v1/query?query=1'))
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = ROOT / 'results' / stamp
    out.mkdir(parents=True)
    env['RESULTS_DIR'] = out.as_posix()
    metadata = {'kind': 'pilot' if args.pilot else 'full', 'control': args.control,
                'planned_duration_seconds': duration, 'created_utc': stamp,
                'vm_url': VM, 'service_url': SERVICE}
    configuration = compose(env, '--profile', 'load', 'config', '--format', 'json').stdout
    (out / 'compose-config.json').write_text(configuration, encoding='utf-8')
    services = json.loads(configuration)['services']
    metadata['configuration'] = services['java']['environment']
    metadata['load_rps'] = float(services['k6']['environment'].get('LOAD_RPS', 250))
    (out / 'metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    print(f'Results: {out}', flush=True)
    show(compose(env, '--profile', 'load', 'rm', '-s', '-f', 'k6', 'java'))
    up(env)
    start = time.time()
    print(f'Starting constant arrival load for {duration}s; control={args.control}', flush=True)
    show(compose(env, '--profile', 'load', 'up', '-d', 'k6'))
    error = None
    last_progress = 0
    try:
        with (out / 'metrics.ndjson').open('w', encoding='utf-8') as samples:
            while time.time() - start < duration + 90:
                record = {'time': time.time()}
                for key, endpoint in [('status', '/status'), ('metrics', '/metrics')]:
                    try:
                        value = http(SERVICE + endpoint)
                        record[key] = json.loads(value) if key == 'status' else value
                    except Exception as exc:
                        record[key + '_error'] = str(exc)
                samples.write(json.dumps(record) + '\n')
                samples.flush()
                status = record.get('status', {})
                if 'experiment_start_uptime_ms' in status and status.get('phase') != 'idle':
                    metadata['experiment_start_uptime_ms'] = status['experiment_start_uptime_ms']
                    metadata['start_time_unix_seconds'] = status['start_time_unix_seconds']
                    metadata['status'] = status
                state = inspect(env, 'k6')
                if state is None:
                    raise RuntimeError('k6 container missing')
                if not state['State']['Running']:
                    if state['State']['ExitCode'] != 0:
                        raise RuntimeError(f'k6 exited with code {state["State"]["ExitCode"]}')
                    break
                if time.time() - last_progress >= 30:
                    print(f'{status.get("elapsed_seconds", 0):.0f}s: {status.get("phase", "starting")}, '
                          f'allocator {status.get("allocation_target_mib_per_second", 0):.1f} MiB/s', flush=True)
                    last_progress = time.time()
                time.sleep(5)
            else:
                raise RuntimeError('Experiment exceeded its planned duration plus 90s')
    except BaseException as exc:
        error = exc
        metadata['runner_error'] = str(exc)
    finally:
        show(compose(env, '--profile', 'load', 'stop', 'k6', check=False))
        # Let the asynchronous raw log writer flush before stopping the Java process.
        time.sleep(2)
        metadata['recording_end_time_unix_seconds'] = time.time()
        (out / 'docker-state.json').write_text(json.dumps(
            {s: inspect(env, s) for s in ['java', 'k6']}, indent=2), encoding='utf-8')
        for service in ['java', 'k6']:
            logs = compose(env, '--profile', 'load', 'logs', '--no-color', service, check=False)
            (out / f'{service}.log').write_text(logs.stdout + logs.stderr, encoding='utf-8')
        show(compose(env, 'stop', 'java', check=False))
        (out / 'metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    if 'start_time_unix_seconds' in metadata:
        parameters = urllib.parse.urlencode({'match[]': '{job="experiment4"}',
                                             'start': metadata['start_time_unix_seconds'],
                                             'end': metadata['recording_end_time_unix_seconds']})
        try:
            (out / 'vm-export.ndjson').write_text(http(VM + '/api/v1/export?' + parameters, 60), encoding='utf-8')
        except Exception as exc:
            metadata['vm_export_error'] = str(exc)
            (out / 'metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    from verify import verify
    report = verify(out)
    (out / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    compact = {key: value for key, value in report.items() if key not in ('ramp_windows', 'micrometer_distribution')}
    compact['micrometer_distribution'] = {key: value for key, value in report['micrometer_distribution'].items()
                                         if key not in ('baseline_probabilities', 'plateau_probabilities')}
    print(json.dumps(compact, indent=2), flush=True)
    if 'start_time_unix_seconds' in metadata:
        begin = int(metadata['start_time_unix_seconds'] * 1000)
        end = int(metadata['recording_end_time_unix_seconds'] * 1000)
        print(f'Grafana: http://localhost:3001/d/experiment4-gc-distribution?from={begin}&to={end}', flush=True)
    if error:
        raise error
    return 0 if report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['up', 'run', 'down'])
    parser.add_argument('--pilot', action='store_true', help='140s calibration; never claims >=15min validation')
    parser.add_argument('--control', action='store_true', help='constant allocation matched control')
    args = parser.parse_args()
    if args.action == 'run':
        return run(args)
    if args.action == 'up':
        up(os.environ.copy())
    else:
        show(compose(os.environ.copy(), '--profile', 'load', 'down'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
