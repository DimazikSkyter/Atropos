"""Offline integrity probes. Run from Atropos root; JSON goes to stdout.

Uses a memory transaction seam for AccountStore validation only. It does not
claim to test PostgreSQL locks, rollback, isolation or persistence.
"""
import copy
import json
from pathlib import Path
import sys

ROOT = next(p for p in (Path.cwd(), *Path(__file__).resolve().parents)
            if (p / 'ui/src/atropos_ui').is_dir())
sys.path.insert(0, str(ROOT / 'ui/src'))
from atropos_ui.detector_runtime import DETECTORS, correlation, detector, replay
from atropos_ui.influx import parse_flux
from atropos_ui.service_api import AccountStore, Datasource
from atropos_detection.services.metric_sets import DETECTOR_NAMES

checks = []


def check(name, fn):
    try:
        detail = fn()
        checks.append({'check': name, 'status': 'PASS', 'detail': detail})
    except AssertionError as exc:
        checks.append({'check': name, 'status': 'FINDING', 'detail': str(exc)})


def registry():
    assert set(DETECTORS) == DETECTOR_NAMES
    for name in DETECTORS:
        detector(name, {})
    return sorted(DETECTORS)


def invalid_time():
    rows = []
    for stamp in ('', 'NaT'):
        csv = ',result,table,_time,_value,_field,_measurement\n,,0,' + stamp + ',1,value,cpu\n'
        try:
            data, _ = parse_flux(csv, 'source')
        except (ValueError, RuntimeError):
            continue
        value = next(iter(data.values()))['series'][0]['times'][0]
        rows.append({'input': stamp, 'accepted_timestamp_ns': value})
    assert not rows, 'Invalid timestamps were accepted: ' + json.dumps(rows)


class MemoryAccount(AccountStore):
    def __init__(self):
        super().__init__('offline-audit', 'postgresql://unused')
        self.workspace = {'metrics': [{'id': 'm', 'name': 'cpu', 'datasource_id': 's',
            'detectors': {}, 'extra_labels': {}, 'drop_labels': [], 'peers': []}]}

    def _edit(self, change, *, expected_revision=None):
        candidate = copy.deepcopy(self.workspace)
        result = change(candidate)
        self.workspace = candidate
        return result


def invalid_bindings():
    accepted = []
    for changes in ({'detectors': {'NotADetector': {}}}, {'extra_labels': {'team': 1}}, {'name': ''}):
        store = MemoryAccount()
        try:
            store.update_metric('m', changes, expected_revision=1)
        except (ValueError, TypeError):
            continue
        accepted.append(changes)
    assert not accepted, 'AccountStore.update_metric accepted: ' + json.dumps(accepted)


def peer_ownership():
    store = MemoryAccount()
    try:
        store.update_metric('m', {'peers': ['foreign-metric']}, expected_revision=1)
    except ValueError:
        return 'Foreign peer rejected; validation seam only'
    assert False, 'Foreign peer accepted'


def exact_timestamps():
    left = {'label': 'left', 'times': [1, 3, 5], 'values': [1, 2, 3]}
    right = {'label': 'right', 'times': [3, 4, 5], 'values': [4, 5, 6]}
    result = correlation({}, [left, right])
    assert result['series'][0]['times'] == [3, 5]
    assert result['series'][0]['scores'] == [None, None]
    return 'Exact intersection; warmup retained as None'


def stale_replay():
    class Store:
        revision = 1
        saved = False

        def snapshot(self):
            source = Datasource('source', 'Source', 'http://unused', 'InfluxDB',
                                organization='org', bucket='bucket')
            from dataclasses import asdict
            return {'sources': [asdict(source)], 'metrics': [{'id': 'm', 'name': 'cpu',
                'datasource_id': 'source', 'measurement': 'cpu', 'field': 'value',
                'detectors': {'MAD': {}}, 'extra_labels': {}, 'drop_labels': []}]}, self.revision

        def save_results(self, result, revision):
            self.saved = True

    store = Store()

    def reader(source):
        store.revision += 1
        return {'m': {'series': [{'labels': {}, 'times': [1], 'values': [1.0]}]}}, 0

    try:
        replay(store, reader=reader)
    except ValueError:
        assert not store.saved
        return 'Stale revision rejected before save_results'
    assert False, 'Stale result published'


for name, fn in [('registry_and_default_constructors', registry),
                 ('reject_invalid_flux_timestamps', invalid_time),
                 ('repository_validates_metric_edits', invalid_bindings),
                 ('repository_rejects_foreign_peer', peer_ownership),
                 ('correlation_exact_intersection', exact_timestamps),
                 ('replay_rejects_stale_revision', stale_replay)]:
    check(name, fn)
print(json.dumps({'checks': checks, 'findings': sum(c['status'] == 'FINDING' for c in checks)}, indent=2))
