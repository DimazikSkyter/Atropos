"""Large-cardinality steady-state validation with independent reachable clones.

Warming 10,000 identical algorithms from scratch measures cold replay instead of
runtime capacity. Here 16 real warmed histories supply templates; mutable state,
NumPy arrays AND float payloads are copied independently. Eight future scores of
each clone are checked against a separately warmed reference. Timed incoming
values are fresh Python floats. No detector code or cached numeric state is edited.
"""
import argparse
import copy
import json
import math
import subprocess
import sys
from pathlib import Path

import benchmark as b


def floats_in(value, seen=None, found=None):
    seen, found = (set() if seen is None else seen), ({} if found is None else found)
    if id(value) in seen or isinstance(value, (type, b.logging.Logger)) or callable(value):
        return found
    seen.add(id(value))
    if isinstance(value, float):
        found[id(value)] = value
    elif isinstance(value, b.np.ndarray):
        pass  # ndarray.__deepcopy__ allocates independent numeric storage.
    elif isinstance(value, dict):
        for key, item in value.items():
            if key != 'logger':
                floats_in(item, seen, found)
    elif isinstance(value, (list, tuple, set, frozenset, b.collections.deque)):
        for item in value:
            floats_in(item, seen, found)
    else:
        if hasattr(value, '__dict__'):
            floats_in(vars(value), seen, found)
        for slot in getattr(type(value), '__slots__', ()):
            if hasattr(value, slot):
                floats_in(getattr(value, slot), seen, found)
    return found


def arrays_in(value, seen=None, found=None):
    seen, found = (set() if seen is None else seen), ([] if found is None else found)
    if id(value) in seen or isinstance(value, (type, b.logging.Logger)) or callable(value):
        return found
    seen.add(id(value))
    if isinstance(value, b.np.ndarray):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key != 'logger':
                arrays_in(item, seen, found)
    elif isinstance(value, (list, tuple, set, frozenset, b.collections.deque)):
        for item in value:
            arrays_in(item, seen, found)
    else:
        if hasattr(value, '__dict__'):
            arrays_in(vars(value), seen, found)
        for slot in getattr(type(value), '__slots__', ()):
            if hasattr(value, slot):
                arrays_in(getattr(value, slot), seen, found)
    return found


def clone_array(array, memo):
    if id(array) in memo:
        return memo[id(array)]
    if isinstance(array.base, b.np.ndarray):
        base = clone_array(array.base, memo)
        offset = array.ctypes.data - array.base.ctypes.data
        result = b.np.ndarray(array.shape, dtype=array.dtype, buffer=base,
                              offset=offset, strides=array.strides)
    else:
        result = array.copy(order='K')
    memo[id(array)] = result
    return result


def clone(template):
    memo = {identity: float.fromhex(value.hex()) for identity, value in floats_in(template).items()}
    for array in arrays_in(template):
        cloned_array = clone_array(array, memo)
        if b.np.shares_memory(array, cloned_array):
            raise AssertionError('Cloned NumPy backing storage is shared')
    result = copy.deepcopy(template, memo)
    if set(floats_in(result)) & set(floats_in(template)):
        raise AssertionError('Cloned float payloads are shared')
    return result


class FreshInput:
    def __init__(self, values):
        self.values = values

    def __getitem__(self, index):
        value = self.values[index]
        return tuple(item + 0.0 for item in value) if isinstance(value, tuple) else value + 0.0


def score(case, state):
    if case['detector'] == 'RuntimeTrend':
        return float(state.evaluate().detected)
    if hasattr(state, 'result'):
        return state.result.score
    return state._score


def run_worker(case):
    b.psutil.Process().cpu_affinity([case['cpu']])
    b.threadpool_limits(limits=1)
    original_factory, original_batch = b.factory, b.batch
    raw = b.input_bank(case, 16)
    templates, template_positions = [], []
    warm = b.warm_count(case)
    for index in range(16):
        state, position = original_factory(case), [0]
        original_batch(case, [state], [raw[index]], position, warm + index % 17)
        assert b.ready(case, state)
        templates.append(state)
        template_positions.append(position[0])
        # The reference is replayed independently from raw history, not cloned.
        reference, reference_position = original_factory(case), [0]
        original_batch(case, [reference], [raw[index]], reference_position, position[0])
        copied, copied_position = clone(state), list(position)
        for _ in range(8):
            original_batch(case, [reference], [raw[index]], reference_position, 1)
            original_batch(case, [copied], [raw[index]], copied_position, 1)
            if not math.isclose(score(case, reference), score(case, copied), rel_tol=1e-11, abs_tol=1e-12):
                raise AssertionError('Independent clone changes scores')
    created, starts = 0, {}

    def cloned_factory(configuration):
        nonlocal created
        index = created % 16
        result = clone(templates[index])
        starts[id(result)] = template_positions[index]
        created += 1
        return result

    def run_batch(configuration, states, banks, positions, rounds):
        for index, state in enumerate(states):
            if positions[index] == 0:
                positions[index] = starts[id(state)]
        original_batch(configuration, states, banks, positions, rounds)

    def large_timed(configuration, states, banks, positions, repeats, seconds):
        # Different templates spread SSA model refits across a large fleet;
        # one fleet pass already includes hundreds of real refits.
        minimum = 1 if len(states) >= 256 else (16 if configuration['detector'] == 'SSA' else 4)
        rounds = minimum
        probe = b.measured(configuration, states, banks, positions, rounds)
        while probe['wall_seconds'] < .1:
            rounds *= 4
            probe = b.measured(configuration, states, banks, positions, rounds)
        rounds = max(minimum, math.ceil(seconds / probe['wall_seconds'] * rounds))
        if configuration['detector'] == 'SSA' and len(states) < 256:
            rounds = math.ceil(rounds / 16) * 16
        return b.summarize([b.measured(configuration, states, banks, positions, rounds) for _ in range(repeats)])

    b.factory = cloned_factory
    b.input_bank = lambda configuration, count: [FreshInput(raw[index % 16]) for index in range(count)]
    b.batch = run_batch
    b.warm_count = lambda configuration: 0
    b.timed_mode = large_timed
    answer = b.worker(case)
    answer['cold'] = None  # This experiment explicitly starts with reachable warm states.
    answer['preparation'] = {'method': 'independent reachable clones', 'templates': 16,
                             'warm_points': warm, 'checked_future_scores_per_template': 8,
                             'independent_float_payloads': True, 'fresh_incoming_floats': True,
                             'numpy_full_backing_storage_preserved': True}
    (b.ROOT / 'results' / (case['id'] + '.json')).write_text(json.dumps(answer, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker')
    parser.add_argument('--only', nargs='*')
    args = parser.parse_args()
    if args.worker:
        run_worker(json.loads(Path(args.worker).read_text(encoding='utf-8')))
        return
    cpu = json.loads((b.ROOT / 'machine.json').read_text())['selected_logical_cpu']
    cases = [{'id': name + '-fleet10000', 'detector': name, 'scenario': 'events', 'fleet': 10_000,
              'width': 2 if name == 'Correlation' else 1, 'cpu': cpu, 'category': 'large_validation'}
             for name in b.CLASSES]
    cases += [
        {'id': 'Trend-curvature-fleet10000', 'detector': 'Trend', 'scenario': 'curvature',
         'fleet': 10_000, 'width': 1, 'cpu': cpu, 'category': 'large_validation'},
        {'id': 'RuntimeTrend-1s-fleet1000', 'detector': 'RuntimeTrend', 'scenario': 'events',
         'fleet': 1000, 'width': 1, 'cpu': cpu, 'sample_seconds': 1, 'category': 'large_validation'},
    ]
    for case in cases:
        if args.only and case['detector'] not in args.only and case['id'] not in args.only:
            continue
        destination = b.ROOT / 'results' / (case['id'] + '.json')
        if destination.exists():
            print('SKIP ' + case['id'], flush=True)
            continue
        case_path = b.ROOT / 'results' / ('case-' + case['id'] + '.json')
        case_path.write_text(json.dumps(case), encoding='utf-8')
        print('RUN ' + case['id'], flush=True)
        finished = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--worker', str(case_path)],
                                  capture_output=True, text=True, encoding='utf-8', timeout=900, check=False)
        if finished.returncode:
            print(finished.stdout + finished.stderr, flush=True)
            raise RuntimeError('Large-fleet validation failed: ' + case['id'])
        answer = json.loads(destination.read_text())
        print(f'DONE {case["id"]}: {answer["fleet"]["median_wall_updates_per_second"]:.1f}/s '
              f'pass={answer["fleet"]["samples"][0]["wall_seconds"]/answer["fleet"]["samples"][0]["rounds"]:.3f}s', flush=True)


if __name__ == '__main__':
    main()
