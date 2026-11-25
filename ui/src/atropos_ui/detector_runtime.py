"""Replay the real detector implementations with fresh state for every series/run."""
import importlib.util
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from uuid import uuid4

from atropos_ui.influx import read_source
from atropos_ui.resource_limits import limit
from atropos_ui.service_api import Datasource, detector_instances, source_reference

PACKAGE = 'atropos_detection'
if PACKAGE not in sys.modules:
    path = Path(__file__).resolve().parents[3] / 'detectors' / 'apps'
    spec = importlib.util.spec_from_file_location(PACKAGE, path / '__init__.py', submodule_search_locations=[str(path)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)

from atropos_detection.registry import (  # noqa: E402
    defaults as defaults,
)
from atropos_detection.registry import (  # noqa: E402
    detector,
    detector_classes,
    run_metadata,
)
from atropos_detection.registry import (  # noqa: E402
    validate_bindings as validate_bindings,
)

DETECTORS = detector_classes()


def labels_for(metric, labels):
    transformed = {k: v for k, v in labels.items() if k not in metric.get('drop_labels', [])}
    transformed.update(metric.get('extra_labels', {}))
    return transformed


def unary(name, parameters, series, *, streams=None, stream_id=None):
    if streams is not None:
        identities = [series['source']['source_series_id']]
        if stream_id is not None:
            identities.append(stream_id)
        return streams.calculate(name, parameters, identities,
                                 series['times'], series['values'])
    instance = detector(name, parameters)
    scores, ready = [], 0
    for stamp, value in zip(series['times'], series['values']):
        if name == 'RuntimeTrend':
            instance.update_ns(stamp, value)
            result = instance.evaluate()
            score = float(result.detected)
        else:
            result = instance.update(value)
            score = float(result.score if hasattr(result, 'score') else result)
        available = getattr(result, 'ready', getattr(instance, 'ready', False))
        ready += int(available)
        scores.append(score if available else None)
    return {'scores': scores, 'ready_points': ready, 'status': 'ok' if ready else 'warmup'}


def correlation(parameters, all_series, *, streams=None, stream_id=None):
    if len(all_series) < 2:
        return {'status': 'skipped', 'message': 'Нужны минимум два ряда. Выберите связанную метрику.', 'series': []}
    # Pairwise exact timestamp intersections preserve gaps and all original samples.
    # No interpolation, index-based pairing, or fabricated self-correlation.
    outputs = []
    for left_index, left in enumerate(all_series):
        if not left.get('target', True):
            continue
        for right in all_series[left_index + 1:]:
            left_values = dict(zip(left['times'], left['values']))
            right_values = dict(zip(right['times'], right['values']))
            common = sorted(left_values.keys() & right_values.keys())
            if streams is not None:
                identities = [left['source']['source_series_id'], right['source']['source_series_id']]
                if stream_id is not None:
                    identities.append(stream_id)
                calculated = streams.calculate('Correlation', parameters, identities, common,
                    [(left_values[t], right_values[t]) for t in common])
                outputs.append({**calculated, 'label': left['label'] + ' ↔ ' + right['label'],
                                'sources': [left['source'], right['source']]})
                continue
            instance = detector('Correlation', parameters)
            scores, ready = [None] * len(common), 0
            for index, stamp in enumerate(common):
                result = instance.update([left_values[stamp], right_values[stamp]])
                position = result.score_index if result.score_index is not None else index
                if result.ready and 0 <= position < len(scores):
                    scores[position] = result.score
                    ready += 1
            outputs.append({'label': left['label'] + ' ↔ ' + right['label'], 'times': common,
                'scores': scores, 'ready_points': ready, 'status': 'ok' if ready else 'warmup'})
            if 'source' in left and 'source' in right:
                outputs[-1]['sources'] = [left['source'], right['source']]
    return {'status': 'ok' if any(s['ready_points'] for s in outputs) else 'warmup', 'series': outputs}


def replay(store, progress=lambda text: None, *, reader=read_source, streams=None):
    workspace, revision = store.snapshot()
    metrics = workspace.get('metrics', [])
    result = {'run_id': uuid4().hex, 'revision': revision, 'started_at': time.time(), 'metrics': {}, 'warnings': [],
              'execution_mode': 'incremental_live' if streams is not None else 'historical_replay'}
    sources = {s['id']: Datasource(**s) for s in workspace.get('sources', [])}
    raw, source_errors = {}, {}
    for source_id in dict.fromkeys(m['datasource_id'] for m in metrics):
        progress('Чтение InfluxDB: ' + sources[source_id].name)
        try:
            data, skipped = reader(sources[source_id])
            if sum(len(s['times']) for m in [*raw.values(), *data.values()] for s in m['series']) > limit('IMPORT_MAX_POINTS', 500_000):
                raise ValueError('Суммарный объём источников превышает ATROPOS_IMPORT_MAX_POINTS.')
            raw.update(data)
            if skipped:
                result['warnings'].append(f'{sources[source_id].name}: пропущено нечисловых/NaN/Inf точек: {skipped}')
        except (ValueError, RuntimeError) as exc:
            source_errors[source_id] = str(exc)
    by_id = {m['id']: m for m in metrics}
    for number, metric in enumerate(metrics, 1):
        uid = metric['id']
        output = result['metrics'][uid] = {'name': metric['name'], 'series': [], 'detectors': {}}
        output.update({k: metric[k] for k in ('datasource_id', 'measurement', 'field', 'query', 'kind') if k in metric})
        if uid not in raw:
            output.update(status='error', message=source_errors.get(metric['datasource_id'], 'Исходная метрика больше не содержит данных.'))
            continue
        series = raw[uid]['series']
        keys = [json.dumps(labels_for(metric, s['labels']), sort_keys=True) for s in series]
        if len(set(keys)) != len(keys):
            output.update(status='error', message='Изменение лейблов делает разные ряды одинаковыми.')
            continue
        output['series'] = [{**s, 'label': json.dumps(labels_for(metric, s['labels']), ensure_ascii=False, sort_keys=True)} for s in series]
        for s in output['series']:
            s['source'] = source_reference(uid, s, metric['name'], metric=metric)
        output['status'] = 'ok'
        for binding in detector_instances(metric.get('detectors', {})):
            name, parameters, binding_id = binding['kind'], binding['parameters'], binding['id']
            stream_id = binding_id if binding_id != name else None
            progress(f'{number}/{len(metrics)} · {metric["name"]} · {binding["name"]}')
            try:
                if name == 'Correlation':
                    related = [{**s, 'label': metric['name'] + ' ' + s['label']} for s in output['series']]
                    for peer_id in metric.get('peers', []):
                        peer = by_id.get(peer_id)
                        for s in raw.get(peer_id, {}).get('series', []):
                            effective = json.dumps(labels_for(peer, s['labels']), ensure_ascii=False, sort_keys=True)
                            related.append({**s, 'target': False,
                                'source': source_reference(peer_id, {**s, 'label': effective}, peer['name'], metric=peer),
                                'label': peer['name'] + ' ' + effective})
                    output['detectors'][binding_id] = correlation(parameters, related, streams=streams, stream_id=stream_id)
                else:
                    outputs = [{**unary(name, parameters, s, streams=streams, stream_id=stream_id),
                                'label': s['label'], 'sources': [s['source']]}
                               for s in output['series']]
                    output['detectors'][binding_id] = {'status': 'ok' if any(s['ready_points'] for s in outputs) else 'warmup', 'series': outputs}
                metadata = run_metadata(name, parameters)
                output['detectors'][binding_id].update(parameters=metadata['effective_parameters'], metadata=metadata)
            except (ValueError, TypeError, ArithmeticError) as exc:
                output['detectors'][binding_id] = {'status': 'error', 'message': str(exc), 'series': [],
                    'metadata': {'requested_parameters': parameters, 'detector': name}}
            output['detectors'][binding_id].update(detector=name, instance_id=binding_id,
                                                   name=binding['name'] if binding_id != name else metric['name'] + '_' + name)
    result['finished_at'] = time.time()
    if store.snapshot()[1] != revision:
        raise ValueError('Настройки изменились во время прогона. Запустите пересчёт заново.')
    store.save_results(result, revision)
    return result


class ReplayRuntime:
    """Per-process jobs and ephemeral results, partitioned by account and revision."""
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='atropos-replay')
        self.lock = RLock()
        self.jobs = {}
        self.restored = {}

    def _prune(self, *, reserve=None):
        now = time.time()
        ttl = limit('REPLAY_CACHE_SECONDS', 3600)
        for key, state in list(self.jobs.items()):
            if state['future'].done() and now - state['last_used'] > ttl:
                del self.jobs[key]
        for key, item in list(self.restored.items()):
            if now - item[2] > ttl:
                del self.restored[key]
        cap = limit('REPLAY_MAX_ACCOUNTS', 64)
        keys = set(self.jobs) | set(self.restored)
        for key in sorted(keys, key=lambda k: self.jobs.get(k, {}).get('last_used', 0)):
            if len(keys | ({reserve} if reserve else set())) <= cap:
                break
            state = self.jobs.get(key)
            if key != reserve and (state is None or state['future'].done()):
                self.jobs.pop(key, None)
                self.restored.pop(key, None)
                keys.remove(key)
        if len(keys | ({reserve} if reserve else set())) > cap:
            raise ValueError('Очередь пересчётов заполнена. Дождитесь завершения текущих прогонов.')

    def start(self, store):
        key = (store._dsn, store.username)
        revision = store.snapshot()[1]
        with self.lock:
            self._prune(reserve=key)
            previous = self.jobs.get(key)
            if previous and not previous['future'].done():
                # The running job notices revision changes and restarts from raw data.
                return previous['revision'] != revision
            state = {'revision': revision, 'progress': 'Запуск', 'error': None, 'result': None,
                     'last_used': time.time()}
            def run():
                while True:
                    state.update(revision=store.snapshot()[1], error=None)
                    try:
                        state['result'] = replay(store, lambda message: state.update(progress=message))
                    except Exception:
                        # Tracebacks may contain source credentials: only a safe message crosses into UI.
                        state['error'] = 'Пересчёт не завершён. Проверьте подключение и параметры; повторите запуск.'
                    if store.snapshot()[1] == state['revision']:
                        break
                    state.update(result=None, progress='Настройки изменились; чтение исходных данных заново')
                return state['result']
            state['future'] = self.pool.submit(run)
            self.jobs[key] = state
            return True

    def state(self, store):
        with self.lock:
            self._prune()
            state = self.jobs.get((store._dsn, store.username))
            if state:
                state['last_used'] = time.time()
            return dict(state) if state else None

    def result(self, store):
        revision = store.snapshot()[1]
        state = self.state(store)
        if state and state['revision'] == revision and state['result'] is not None:
            state['last_used'] = time.time()
            return state['result']
        key = (store._dsn, store.username)
        with self.lock:
            self._prune(reserve=key)
            previous = self.restored.get(key)
            if previous and previous[0] == revision:
                self.restored[key] = (*previous[:2], time.time())
                return previous[1]
            result = store.load_results()
            if result is not None:
                self.restored[key] = (revision, result, time.time())
            return result
