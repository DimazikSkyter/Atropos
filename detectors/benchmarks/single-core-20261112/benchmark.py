"""Single-CPU benchmarks of unmodified Atropos detectors.

Children are pinned to one logical CPU, with BLAS/OpenMP limited to one thread.
Input creation, imports, warmup and profiling are outside throughput timings.
The two hot modes retain detector state: one history and an independent fleet.
"""
# ruff: noqa: E402 -- native thread limits must be set before NumPy/SciPy imports.
import os

for variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                 'BLIS_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[variable] = '1'

import argparse
import collections
import cProfile
import ctypes
import gc
import gzip
import inspect
import io
import itertools
import json
import logging
import math
import platform
import pstats
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import psutil
import scipy
from threadpoolctl import threadpool_info, threadpool_limits

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'snapshot'))
from apps.detectors import (  # noqa: E402
    ACFDetector,
    CorrelationDetector,
    CusumDetector,
    MadDetector,
    PredictionIntervalDetector,
    SSADetector,
    TrendDetector,
)
from apps.runtime_trend import RuntimeTrendDetector, TrendConfig  # noqa: E402

CLASSES = {'Trend': TrendDetector, 'CUSUM': CusumDetector, 'MAD': MadDetector,
           'SSA': SSADetector, 'ACF': ACFDetector, 'Correlation': CorrelationDetector,
           'PredictionInterval': PredictionIntervalDetector, 'RuntimeTrend': RuntimeTrendDetector}
DEFAULT_RUNTIME = {'window_seconds': 120.0, 'variance_threshold': 1.0, 'min_tangent': 1.0}
BASE_WARM = {'Trend': 256, 'CUSUM': 320, 'MAD': 128, 'SSA': 992,
             'ACF': 800, 'Correlation': 160, 'PredictionInterval': 64}
MASK = 4095


def cpu_topology():
    if os.name != 'nt':
        return [{'group': 0, 'logical_cpus': [i]} for i in psutil.Process().cpu_affinity()]
    length = ctypes.c_ulong(0)
    api = ctypes.windll.kernel32.GetLogicalProcessorInformationEx
    api(0, None, ctypes.byref(length))
    buffer = ctypes.create_string_buffer(length.value)
    if not api(0, buffer, ctypes.byref(length)):
        raise ctypes.WinError()
    raw = buffer.raw
    result, offset = [], 0
    while offset < length.value:
        relationship, size = struct.unpack_from('<II', raw, offset)
        if relationship == 0:
            groups = struct.unpack_from('<H', raw, offset + 30)[0]
            for index in range(groups):
                mask, group = struct.unpack_from('<QH', raw, offset + 32 + 16 * index)
                result.append({'group': group, 'logical_cpus': [i for i in range(64) if mask & (1 << i)]})
        offset += size
    return result


def choose_cpu():
    topology = cpu_topology()
    usage = psutil.cpu_percent(interval=1.5, percpu=True)
    allowed = set(psutil.Process().cpu_affinity())
    candidates = [{**core, 'logical_cpus': [i for i in core['logical_cpus'] if i in allowed]}
                  for core in topology if core['group'] == 0 and any(i in allowed for i in core['logical_cpus'])]
    candidates = [core for core in candidates if 0 not in core['logical_cpus']] or candidates
    chosen = min(candidates, key=lambda core: max(usage[i] for i in core['logical_cpus']))
    cpu = min(chosen['logical_cpus'], key=lambda i: usage[i])
    return cpu, {'topology': topology, 'initial_utilization_percent': usage,
                 'selected_physical_core_logicals': chosen['logical_cpus'], 'selected_logical_cpu': cpu}


def defaults(name):
    target = TrendConfig if name == 'RuntimeTrend' else CLASSES[name]
    result = {name: value.default for name, value in inspect.signature(target).parameters.items()
              if value.default is not inspect.Parameter.empty and not name.startswith('logger')}
    if name == 'RuntimeTrend':
        result.update(DEFAULT_RUNTIME)
    return result


def factory(case):
    name = case['detector']
    parameters = case.get('parameters', {})
    if name == 'RuntimeTrend':
        return RuntimeTrendDetector(TrendConfig(**{**DEFAULT_RUNTIME, **parameters}))
    if name == 'Correlation' and case.get('pairwise_ui'):
        return [CorrelationDetector(**parameters) for _ in itertools.combinations(range(case.get('width', 2)), 2)]
    return CLASSES[name](**parameters)


def warm_count(case):
    name = case['detector']
    p = {**defaults(name), **case.get('parameters', {})}
    if name in ('Trend', 'MAD', 'SSA'):
        return p['data_max_size'] + (32 if name == 'SSA' else 16)
    if name == 'ACF':
        return p['reference_window'] + p['window'] + 32
    if name == 'CUSUM':
        return max(320, p['reference_window'] + 2 * p['window'] + (p['inertia_window'] or 20 * p['window']))
    if name == 'PredictionInterval':
        return p['window'] + 32
    if name == 'Correlation':
        return max(p['reference_window'], p['window']) + 32
    return max(math.ceil(p['window_seconds'] / case.get('sample_seconds', 15)) + 1, p['min_samples']) + 16


def real_inputs():
    payload = json.loads(gzip.decompress((ROOT / 'influx-input.json.gz').read_bytes()))
    wanted = ['cassandra_response_time', 'screenshot_step_replay', 'service_response_time', 'kafka_write_time']
    available = {m['measurement']: m for m in payload['metrics'].values() if m['field'] == 'value'}
    return [available[name]['series'][0]['values'] for name in wanted], available['cassandra_response_time']['series']


def input_bank(case, fleet):
    scenario, width = case['scenario'], case.get('width', 1)
    real, cassandra = real_inputs() if scenario == 'influx' else (None, None)
    bank = []
    indices = np.arange(MASK + 1, dtype=float)
    for stream in range(fleet):
        rng = np.random.default_rng(41703 + stream)
        if scenario == 'influx':
            if width > 1:
                columns = [[float(cassandra[j % len(cassandra)]['values'][i % len(cassandra[j % len(cassandra)]['values'])]) + 0.0
                            for i in range(MASK + 1)] for j in range(width)]
                values = list(zip(*columns))
            else:
                original = real[stream % len(real)]
                values = [float(original[i % len(original)]) + 0.0 for i in range(MASK + 1)]
        else:
            noise = rng.normal(0, 0.45, MASK + 1)
            base = 100 + 0.001 * stream + np.sin(indices / 11) + 0.3 * np.sin(indices / 47) + noise
            if scenario == 'curvature':
                # Smooth curvature accumulates evidence but is rejected by the
                # breakpoint-vs-cubic check, exercising repeated O(window) scans.
                base = 100 + 20 * np.sin(indices / 170) + rng.normal(0, .001, MASK + 1)
            if scenario == 'events':
                phase = indices.astype(int) % 512
                base += np.where(phase < 160, phase * .08, 0)
                base += np.where((phase >= 256) & (phase < 384), 10, 0)
                base += np.where((indices.astype(int) % 97) < 2, 25, 0)
            if width > 1:
                columns = []
                for column in range(width):
                    member = np.roll(base, column % 3) * (1 + .1 * column) + rng.normal(0, .15, MASK + 1)
                    if scenario == 'events' and column % 2:
                        member = np.where((indices.astype(int) % 512) >= 384, 210 - member, member)
                    columns.append(member.tolist())
                values = list(zip(*columns))
            else:
                values = base.tolist()
        bank.append(values)
    return bank


def batch(case, states, bank, positions, rounds):
    name = case['detector']
    count = len(states)
    if name == 'RuntimeTrend':
        interval = case.get('sample_seconds', 15)
        stride = case.get('evaluate_stride', 1)
        for _ in range(rounds):
            for index in range(count):
                position = positions[index]
                state = states[index]
                state.update(position * interval, bank[index][position & MASK])
                if position % stride == 0:
                    state.evaluate()
                positions[index] = position + 1
    elif name == 'Correlation' and case.get('pairwise_ui'):
        pairs = tuple(itertools.combinations(range(case.get('width', 2)), 2))
        for _ in range(rounds):
            for index in range(count):
                position = positions[index]
                row = bank[index][position & MASK]
                for state, (left, right) in zip(states[index], pairs):
                    state.update((row[left], row[right]))
                positions[index] = position + 1
    else:
        for _ in range(rounds):
            for index in range(count):
                position = positions[index]
                states[index].update(bank[index][position & MASK])
                positions[index] = position + 1


def ready(case, state):
    if isinstance(state, list):
        return all(item.ready for item in state)
    if case['detector'] == 'RuntimeTrend':
        return state.evaluate().ready
    return state.ready


def object_bytes(value, seen=None):
    """Reachable state graph; shared logger/code excluded, NumPy backing arrays included."""
    seen = set() if seen is None else seen
    if id(value) in seen or isinstance(value, (type, logging.Logger)) or callable(value):
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, np.ndarray):
        return size + (object_bytes(value.base, seen) if value.base is not None else 0)
    if isinstance(value, dict):
        return size + sum(object_bytes(key, seen) + object_bytes(item, seen) for key, item in value.items() if key != 'logger')
    if isinstance(value, (list, tuple, set, frozenset, collections.deque)):
        return size + sum(object_bytes(item, seen) for item in value)
    if hasattr(value, '__dict__'):
        size += object_bytes(vars(value), seen)
    for slot in getattr(type(value), '__slots__', ()):
        if hasattr(value, slot):
            size += object_bytes(getattr(value, slot), seen)
    return size


def measured(case, states, bank, positions, rounds):
    start_cpu, start = time.process_time_ns(), time.perf_counter_ns()
    batch(case, states, bank, positions, rounds)
    wall_ns, cpu_ns = time.perf_counter_ns() - start, time.process_time_ns() - start_cpu
    count = len(states) * rounds
    return {'updates': count, 'rounds': rounds, 'fleet': len(states), 'wall_seconds': wall_ns / 1e9,
            'cpu_seconds': cpu_ns / 1e9, 'wall_updates_per_second': count * 1e9 / wall_ns,
            'cpu_updates_per_second': count * 1e9 / cpu_ns if cpu_ns else None,
            'cpu_us_per_update': cpu_ns / count / 1000, 'cpu_wall_ratio': cpu_ns / wall_ns}


def summarize(samples):
    return {'samples': samples,
            'median_wall_updates_per_second': statistics.median(s['wall_updates_per_second'] for s in samples),
            'min_wall_updates_per_second': min(s['wall_updates_per_second'] for s in samples),
            'max_wall_updates_per_second': max(s['wall_updates_per_second'] for s in samples),
            'aggregate_cpu_us_per_update': sum(s['cpu_seconds'] for s in samples) * 1e6 / sum(s['updates'] for s in samples),
            'aggregate_cpu_wall_ratio': sum(s['cpu_seconds'] for s in samples) / sum(s['wall_seconds'] for s in samples)}


def timed_mode(case, states, bank, positions, repeats, seconds):
    minimum = 16 if case['detector'] == 'SSA' else 4
    probe = measured(case, states, bank, positions, minimum)
    probe_rounds = minimum
    while probe['wall_seconds'] < .1:
        probe_rounds *= 4
        probe = measured(case, states, bank, positions, probe_rounds)
    rounds = max(minimum, math.ceil(seconds / probe['wall_seconds'] * probe_rounds))
    if case['detector'] == 'SSA':
        rounds = math.ceil(rounds / 16) * 16
    return summarize([measured(case, states, bank, positions, rounds) for _ in range(repeats)])


def worker(case):
    psutil.Process().cpu_affinity([case['cpu']])
    threadpool_limits(limits=1)
    gc.enable()
    name = case['detector']
    fleet = case.get('fleet', 64)
    bank = input_bank(case, fleet)
    start = time.perf_counter()
    states = [factory(case) for _ in range(fleet)]
    positions = [0] * fleet
    warm = warm_count(case)
    for index in range(fleet):
        local = [positions[index]]
        batch(case, [states[index]], [bank[index]], local, warm + index % 17)
        positions[index] = local[0]
    if not all(ready(case, state) for state in states):
        raise AssertionError('A warmed-state benchmark must not measure the no-op warmup path')
    init_seconds = time.perf_counter() - start
    state_before = object_bytes(states) / fleet
    single_positions = [positions[0]]
    single = timed_mode(case, [states[0]], [bank[0]], single_positions, case.get('repeats', 5), case.get('seconds', .5))
    positions[0] = single_positions[0]
    many = timed_mode(case, states, bank, positions, case.get('repeats', 5), case.get('seconds', .5)) if fleet > 1 else None
    state_after = object_bytes(states) / fleet
    latency_positions = [positions[0]]
    latency = []
    for _ in range(256):
        before = time.perf_counter_ns()
        batch(case, [states[0]], [bank[0]], latency_positions, 1)
        latency.append((time.perf_counter_ns() - before) / 1000)
    cold = factory(case)
    cold_positions = [0]
    first_ready = None
    cold_start = time.perf_counter()
    for index in range(warm):
        batch(case, [cold], [bank[0]], cold_positions, 1)
        if first_ready is None and ready(case, cold):
            first_ready = {'points': index + 1, 'wall_seconds': time.perf_counter() - cold_start}
    cold_seconds = time.perf_counter() - cold_start
    profile = None
    if case.get('profile'):
        profiler = cProfile.Profile()
        rounds = max(32, round(single['median_wall_updates_per_second'] * .2))
        profiler.enable()
        batch(case, [states[0]], [bank[0]], latency_positions, rounds)
        profiler.disable()
        destination = ROOT / 'results' / f'profile-{case["id"]}'
        profiler.dump_stats(str(destination.with_suffix('.prof')))
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats('cumulative').print_stats(24)
        pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats('tottime').print_stats(18)
        destination.with_suffix('.txt').write_text(stream.getvalue(), encoding='utf-8')
        profile = destination.with_suffix('.txt').name
    return {'case': case, 'parameters': {**defaults(name), **case.get('parameters', {})},
            'environment': {'python': sys.version, 'numpy': np.__version__, 'scipy': scipy.__version__,
                'affinity': psutil.Process().cpu_affinity(), 'threadpool_info': threadpool_info(),
                'gc_enabled': gc.isenabled(), 'logical_threads': psutil.Process().num_threads(),
                'clocks': {name: vars(time.get_clock_info(name)) for name in ('perf_counter', 'process_time')}},
            'fleet_init_seconds': init_seconds, 'warm_points_per_instance': warm,
            'ready_instances': sum(ready(case, state) for state in states), 'single': single, 'fleet': many,
            'wall_latency_us': {'p50': float(np.percentile(latency, 50)), 'p95': float(np.percentile(latency, 95)),
                'p99': float(np.percentile(latency, 99)), 'max': max(latency), 'samples': len(latency)},
            'state_bytes_per_instance_before': state_before, 'state_bytes_per_instance_after': state_after,
            'process_memory': psutil.Process().memory_info()._asdict(),
            'cold': {'first_ready': first_ready, 'total_points': warm, 'total_wall_seconds': cold_seconds},
            'profile_file': profile}


def cases(cpu):
    result = []
    for scenario in ('quiet', 'events', 'influx'):
        for name in CLASSES:
            result.append({'id': f'{name}-{scenario}', 'detector': name, 'scenario': scenario,
                           'width': 2 if name == 'Correlation' else 1, 'cpu': cpu, 'fleet': 64,
                           'profile': scenario == 'events', 'category': 'baseline'})
    for name in CLASSES:
        result.append({'id': f'{name}-fleet256', 'detector': name, 'scenario': 'events',
                       'width': 2 if name == 'Correlation' else 1, 'cpu': cpu, 'fleet': 256, 'category': 'large_fleet'})
    variations = [
        ('Trend-curvature', 'Trend', {}, {'scenario': 'curvature', 'fleet': 64, 'profile': True}),
        ('Trend-curvature-large', 'Trend', {'window': 160, 'data_max_size': 960},
         {'scenario': 'curvature', 'fleet': 64, 'profile': True}),
        ('Trend-large', 'Trend', {'window': 160, 'data_max_size': 960}, {}),
        ('CUSUM-large', 'CUSUM', {'window': 40, 'reference_window': 400, 'min_reference_samples': 80}, {}),
        ('MAD-large', 'MAD', {'window': 80, 'data_max_size': 400}, {}),
        ('SSA-small', 'SSA', {'window': 64, 'data_max_size': 320}, {}),
        ('SSA-refit1', 'SSA', {'refit_interval': 1}, {}),
        ('SSA-frozen', 'SSA', {'refit_interval': None}, {}),
        ('ACF-small', 'ACF', {'window': 64, 'max_lag': 8, 'reference_window': 128}, {}),
        ('ACF-large', 'ACF', {'window': 1024, 'max_lag': 128, 'reference_window': 2048}, {}),
        ('Correlation-lag16', 'Correlation', {'window': 64, 'max_lag': 16, 'reference_window': 256}, {'width': 2}),
        ('Correlation-alignment2', 'Correlation', {'alignment_tolerance': 2}, {'width': 2}),
        ('Correlation-group4', 'Correlation', {}, {'width': 4}),
        ('Correlation-group8', 'Correlation', {}, {'width': 8}),
        ('Correlation-UIgroup4', 'Correlation', {}, {'width': 4, 'pairwise_ui': True}),
        ('Correlation-UIgroup8', 'Correlation', {}, {'width': 8, 'pairwise_ui': True}),
        ('PredictionInterval-large', 'PredictionInterval', {'window': 120}, {}),
        ('RuntimeTrend-1s', 'RuntimeTrend', {}, {'sample_seconds': 1, 'fleet': 64}),
        ('RuntimeTrend-10min', 'RuntimeTrend', {'window_seconds': 600}, {}),
        ('RuntimeTrend-30min', 'RuntimeTrend', {'window_seconds': 1800}, {}),
        ('RuntimeTrend-eval60s', 'RuntimeTrend', {}, {'evaluate_stride': 4, 'fleet': 64}),
    ]
    for identifier, name, parameters, extras in variations:
        result.append({'id': identifier, 'detector': name, 'scenario': 'events', 'parameters': parameters,
                       'cpu': cpu, 'fleet': 1, 'width': 2 if name == 'Correlation' else 1,
                       'category': 'sensitivity', **extras})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker')
    parser.add_argument('--cpu', type=int)
    parser.add_argument('--only', nargs='*')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if args.worker:
        case = json.loads(Path(args.worker).read_text(encoding='utf-8-sig'))
        answer = worker(case)
        (ROOT / 'results' / (case['id'] + '.json')).write_text(json.dumps(answer, indent=2), encoding='utf-8')
        return
    if args.resume and (ROOT / 'machine.json').exists():
        metadata = json.loads((ROOT / 'machine.json').read_text(encoding='utf-8'))
        cpu = args.cpu if args.cpu is not None else metadata['selected_logical_cpu']
        if cpu != metadata['selected_logical_cpu']:
            raise ValueError('Do not mix CPU affinities in one results directory; use a fresh copy for --cpu changes')
    else:
        cpu, metadata = choose_cpu()
        cpu = args.cpu if args.cpu is not None else cpu
        if cpu not in psutil.Process().cpu_affinity():
            raise ValueError('Selected CPU is outside the process affinity')
        metadata['selected_physical_core_logicals'] = next(
            (core['logical_cpus'] for core in metadata['topology'] if core['group'] == 0 and cpu in core['logical_cpus']), [cpu])
        metadata.update(selected_logical_cpu=cpu, platform=platform.platform(), processor=platform.processor(),
                        python=sys.version, total_memory=psutil.virtual_memory().total,
                        started_at_unix=time.time(), native_thread_limits=1,
                        source_manifest=json.loads((ROOT / 'source-manifest.json').read_text(encoding='utf-8-sig')))
        (ROOT / 'machine.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    all_cases = cases(cpu)
    if args.only:
        all_cases = [case for case in all_cases if case['id'] in args.only]
    for number, case in enumerate(all_cases, 1):
        destination = ROOT / 'results' / (case['id'] + '.json')
        if args.resume and destination.exists():
            print('SKIP ' + case['id'], flush=True)
            continue
        print(f'RUN {number}/{len(all_cases)} {case["id"]} CPU={cpu} fleet={case["fleet"]}', flush=True)
        case_path = ROOT / 'results' / ('case-' + case['id'] + '.json')
        case_path.write_text(json.dumps(case), encoding='utf-8')
        completed = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--worker', str(case_path)],
                                   capture_output=True, text=True, encoding='utf-8', timeout=600, check=False)
        if completed.returncode:
            print(completed.stdout + completed.stderr, flush=True)
            raise RuntimeError('Benchmark failed: ' + case['id'])
        answer = json.loads(destination.read_text(encoding='utf-8'))
        single = answer['single']['median_wall_updates_per_second']
        fleet_result = answer['fleet']
        many = fleet_result['median_wall_updates_per_second'] if fleet_result else None
        print(f'DONE {case["id"]}: history={single:.1f}/s fleet={many} cpu/wall={answer["single"]["aggregate_cpu_wall_ratio"]:.3f}', flush=True)
    print('Benchmark cases complete.', flush=True)


if __name__ == '__main__':
    main()
