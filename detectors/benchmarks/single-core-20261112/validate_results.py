"""Audit the experiment's source identity, CPU limits and score-preserving states."""
import gzip
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath

import benchmark as b
import large_fleet as large


def main():
    root = Path(__file__).resolve().parent
    machine = json.loads((root / 'machine.json').read_text())
    cpu = machine['selected_logical_cpu']
    rows = [json.loads(path.read_text()) for path in (root / 'results').glob('*.json')
            if not path.name.startswith('case-')]
    expected = {case['id'] for case in b.cases(cpu)}
    expected.update(name + '-fleet10000' for name in b.CLASSES)
    expected.update(('Trend-curvature-fleet10000', 'RuntimeTrend-1s-fleet1000'))
    assert {row['case']['id'] for row in rows} == expected
    occupancy = []
    for row in rows:
        assert row['environment']['affinity'] == [cpu]
        assert all(item['num_threads'] == 1 for item in row['environment']['threadpool_info'])
        assert row['ready_instances'] == row['case']['fleet']
        for mode in ('single', 'fleet'):
            if row[mode]:
                assert len(row[mode]['samples']) == 5
                occupancy.append(row[mode]['aggregate_cpu_wall_ratio'])
                for sample in row[mode]['samples']:
                    assert sample['updates'] == sample['rounds'] * sample['fleet']
                    assert math.isclose(sample['updates'] / sample['wall_seconds'], sample['wall_updates_per_second'])
        if row['case']['id'] == 'SSA-fleet10000':
            assert row['preparation']['numpy_full_backing_storage_preserved']
    assert min(occupancy) > .8 and max(occupancy) < 1.1
    manifest = json.loads((root / 'source-manifest.json').read_text(encoding='utf-8-sig'))
    for record in manifest:
        path = root / 'snapshot' / 'apps' / PureWindowsPath(record['path']).name
        assert hashlib.sha256(path.read_bytes()).hexdigest().lower() == record['sha256'].lower()
    data = (root / 'influx-input.json.gz').read_bytes()
    inputs = json.loads((root / 'influx-manifest.json').read_text())
    assert hashlib.sha256(data).hexdigest() == inputs['gzip_sha256']
    assert hashlib.sha256(gzip.decompress(data)).hexdigest() == inputs['decoded_sha256']
    clone_checks = []
    for name in b.CLASSES:
        case = {'detector': name, 'scenario': 'quiet', 'width': 2 if name == 'Correlation' else 1}
        bank = b.input_bank(case, 1)
        original, position = b.factory(case), [0]
        b.batch(case, [original], bank, position, b.warm_count(case))
        cloned, clone_position = large.clone(original), list(position)
        source_arrays, cloned_arrays = large.arrays_in(original), large.arrays_in(cloned)
        assert len(source_arrays) == len(cloned_arrays)
        for left, right in zip(source_arrays, cloned_arrays):
            assert not b.np.shares_memory(left, right)
            assert left.shape == right.shape and left.strides == right.strides
            if isinstance(left.base, b.np.ndarray):
                assert right.base.shape == left.base.shape
        for _ in range(8):
            b.batch(case, [original], bank, position, 1)
            b.batch(case, [cloned], bank, clone_position, 1)
            assert math.isclose(large.score(case, original), large.score(case, cloned), rel_tol=1e-11, abs_tol=1e-12)
        clone_checks.append({'detector': name, 'future_scores_equal': 8,
                             'numpy_arrays_independent': len(source_arrays), 'full_array_layout_preserved': True})
    state = b.RuntimeTrendDetector(b.TrendConfig(**b.DEFAULT_RUNTIME))
    ready_count = 0
    for index in range(1024):
        state.update(index * 60, 100 + math.sin(index))
        ready_count += int(state.evaluate().ready)
    assert ready_count == 0 and len(state._points) == 3
    answer = {'passed': True, 'cases': len(rows), 'affinity': [cpu], 'all_blas_threads': 1,
              'cpu_wall_ratio_range': [min(occupancy), max(occupancy)], 'clone_checks': clone_checks,
              'runtime_default_at_60s': {'updates': 1024, 'ready_count': ready_count,
                                         'retained_points': len(state._points), 'required_min_samples': 8},
              'source_snapshot_hashes_match': True, 'influx_hashes_match': True}
    (root / 'validation.json').write_text(json.dumps(answer, indent=2), encoding='utf-8')
    print(json.dumps(answer, indent=2))


if __name__ == '__main__':
    main()
