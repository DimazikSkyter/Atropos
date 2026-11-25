"""Process-owned rolling-window jobs. Workers never depend on a browser session."""
import copy
import hashlib
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event, RLock, Thread
from uuid import uuid4

import pandas as pd

from atropos_ui.detector_runtime import detector, replay
from atropos_ui.metric_reader import read_metric_window
from atropos_ui.resource_limits import limit
from atropos_ui.service_api import (
    catalog_metrics,
    collect_components,
    detector_instances,
    source_reference,
    validate_metric_set,
)
from atropos_ui.streaming import DetectorStreams
from atropos_ui.time_ranges import TimeRange


def window_bounds(item, now):
    first, last = TimeRange(item['start'], item['end']).resolve(now=datetime.fromtimestamp(now, UTC))
    return int(round(first * 1e9)), int(round(last * 1e9))


def prepare_set(workspace, set_id):
    item = next((s for s in workspace.get('metric_sets', []) if s['id'] == set_id), None)
    if item is None:
        raise ValueError('Сет не найден в этой учётке.')
    available = catalog_metrics(workspace)
    item = validate_metric_set(item, available)
    members = {m['metric_id']: m for m in item['members']}
    required = set(members) | {p for m in members.values() for p in m['peers']
                               if any(b['kind'] == 'Correlation' for b in detector_instances(m['detectors']))}
    metrics = []
    for metric in available:
        if metric['id'] not in required:
            continue
        member = members.get(metric['id'], {})
        metrics.append({**copy.deepcopy(metric), 'detectors': copy.deepcopy(member.get('detectors', {})),
                        'peers': list(member.get('peers', []))})
    source_ids = {m['datasource_id'] for m in metrics}
    sources = [copy.deepcopy(s) for s in workspace.get('sources', []) if s['id'] in source_ids]
    if source_ids - {s['id'] for s in sources}:
        raise ValueError('Источник сета недоступен.')
    plan = {'metrics': metrics, 'sources': sources}
    # Discovery counts and the historical replay's detector bindings do not affect this set.
    signature = {'set': item, 'sources': sources, 'metrics': [
        {k: v for k, v in m.items() if k != 'series'} for m in metrics]}
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return item, plan, fingerprint


class WindowStore:
    """Reuse the real replay without persisting or changing account configuration."""
    def __init__(self, workspace):
        self.workspace = workspace

    def snapshot(self):
        return self.workspace, 0

    def save_results(self, result, revision):
        return False


class LiveHistory:
    """Deduplicated source history plus immutable reactions from every evaluation."""
    def __init__(self, *, max_runs=None, max_points=None, retention_seconds=None):
        self.sources = {}
        self.components = {}
        self.runs = []
        self.reaction_points = 0
        self.max_runs = max_runs or limit('LIVE_HISTORY_RUNS', 240)
        self.max_points = max_points or limit('LIVE_HISTORY_POINTS', 250_000)
        self.retention_seconds = retention_seconds or limit('LIVE_RETENTION_SECONDS', 86400)
        self.evicted_runs = 0

    def append(self, result, bounds):
        components, warnings = collect_components(result)
        for uid, metric in result['metrics'].items():
            for series in metric['series']:
                reference = source_reference(uid, series, metric['name'], metric=metric)
                target = self.sources.setdefault(reference['source_series_id'], {**reference, 'points': {}})
                target['points'].update(zip(series['times'], series['values']))
        for component in components:
            self.components[component['id']] = {k: copy.deepcopy(v) for k, v in component.items()
                                                 if k not in {'times', 'scores'}}
        # Each run owns its score arrays: a later warmup/recalculation never erases an earlier run.
        self.runs.append({'bounds': bounds, 'finished_at': result['finished_at'], 'warnings': warnings,
                          'components': components, 'metadata': {
            **{k: result[k] for k in ('run_id', 'revision', 'execution_mode', 'started_at', 'finished_at',
                                     'session_id', 'configuration_fingerprint') if k in result},
            'bounds_ns': list(bounds), 'metrics': {uid: {
                **{k: m[k] for k in ('name', 'datasource_id', 'measurement', 'field') if k in m}, 'detectors': {
                name: {**{k: copy.deepcopy(v) for k, v in output.items() if k != 'series'},
                       'series_summary': [{k: s[k] for k in ('label', 'ready_points', 'status') if k in s}
                                          for s in output.get('series', [])]}
                for name, output in m['detectors'].items()}} for uid, m in result['metrics'].items()}}})
        self._prune()

    def _prune(self):
        cutoff = self.runs[-1]['bounds'][1] - self.retention_seconds * 1_000_000_000
        while len(self.runs) > 1 and (len(self.runs) > self.max_runs or self.runs[0]['bounds'][1] < cutoff):
            self.runs.pop(0)
            self.evicted_runs += 1
        cutoff = max(cutoff, min([self.runs[0]['bounds'][0], *[
            t for c in self.runs[0]['components'] for t in c['times']]]))
        def trim(first):
            for source in self.sources.values():
                source['points'] = {t: v for t, v in source['points'].items() if t >= first}
            for run in self.runs:
                for component in run['components']:
                    pairs = [(t, v) for t, v in zip(component['times'], component['scores']) if t >= first]
                    component.update(times=[t for t, _ in pairs], scores=[v for _, v in pairs])
        trim(cutoff)
        counts = Counter(t for source in self.sources.values() for t in source['points'])
        counts.update(t for run in self.runs for c in run['components'] for t in c['times'])
        total = sum(counts.values())
        for stamp in sorted(counts):
            if total <= self.max_points:
                break
            total -= counts[stamp]
            cutoff = stamp + 1
        trim(cutoff)
        self.components = {c['id']: {k: copy.deepcopy(v) for k, v in c.items() if k not in {'times', 'scores'}}
                           for run in self.runs for c in run['components'] if c['times']}
        self.sources = {uid: s for uid, s in self.sources.items() if s['points']}
        self.reaction_points = sum(len(c['scores']) for r in self.runs for c in r['components'])

    def snapshot(self, selected, run=None):
        chosen = set(selected)
        runs = self.runs if run is None else [self.runs[run]]
        points = {uid: {} for uid in chosen if uid in self.components}
        for cycle in runs:
            for component in cycle['components']:
                if component['id'] not in points:
                    continue
                target = points[component['id']]
                for stamp, score in zip(component['times'], component['scores']):
                    if score is not None or stamp not in target:
                        target[stamp] = score
        components = []
        for uid, values in points.items():
            stamps = sorted(values)
            components.append({**copy.deepcopy(self.components[uid]), 'times': stamps,
                               'scores': [values[t] for t in stamps]})
        references = {s['source_series_id'] for c in components for s in c['sources']}
        references.update(uid for uid in self.sources if not chosen or uid in chosen)
        sources = []
        bounds = self.runs[run]['bounds'] if run is not None else None
        for uid in sorted(references):
            source = self.sources.get(uid)
            if source is None:
                continue
            stamps = sorted(t for t in source['points'] if bounds is None or bounds[0] <= t < bounds[1])
            sources.append({**copy.deepcopy({k: v for k, v in source.items() if k != 'points'}),
                            'times': stamps, 'values': [source['points'][t] for t in stamps]})
        return {'components': components, 'sources': sources}


class LiveRuntime:
    """Bounded, non-overlapping jobs; paused sessions retain all their history."""
    def __init__(self, *, reader=read_metric_window, clock=time.time, max_workers=2, autostart=True):
        self.reader, self.clock = reader, clock
        self.max_workers = max_workers
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='atropos-live')
        self.lock = RLock()
        self.sessions = {}
        self.active = {}
        self.closed = Event()
        self.thread = Thread(target=self._loop, name='atropos-live-scheduler', daemon=True)
        if autostart:
            self.thread.start()

    @staticmethod
    def account_key(store):
        return store._dsn, store.username

    def start(self, store, set_id):
        workspace, revision = store.snapshot()
        item, plan, fingerprint = prepare_set(workspace, set_id)
        window_bounds(item, self.clock())
        for member in item['members']:
            detector_instances(member['detectors'])
        key = (*self.account_key(store), set_id)
        with self.lock:
            if self.closed.is_set():
                raise ValueError('Рантайм остановлен.')
            previous = self.sessions.get(self.active.get(key))
            if previous and previous['fingerprint'] == fingerprint:
                if previous['running']:
                    return False
                previous.update(running=True, next_at=self.clock(), error=None, revision=revision)
                return True
            # Evict only idle sessions; a running/in-flight calculation is never removed.
            cap = limit('LIVE_MAX_SESSIONS', 64)
            per_account = limit('LIVE_ACCOUNT_SESSIONS', 16)
            idle = sorted((s for s in self.sessions.values() if not s['running']
                           and (s['future'] is None or s['future'].done())), key=lambda s: s['created_at'])
            for stale in idle:
                account_full = sum(s['account'] == self.account_key(store) for s in self.sessions.values()) >= per_account
                expired = self.clock() - stale['created_at'] > limit('LIVE_RETENTION_SECONDS', 86400)
                if expired or len(self.sessions) >= cap or (account_full and stale['account'] == self.account_key(store)):
                    self.sessions.pop(stale['id'])
                    self.active = {k: v for k, v in self.active.items() if v != stale['id']}
            if (len(self.sessions) >= cap or
                    sum(s['account'] == self.account_key(store) for s in self.sessions.values()) >= per_account):
                raise ValueError('Достигнут лимит live-сессий. Остановите и удалите ненужные сессии.')
            if previous:
                previous['running'] = False
            uid = uuid4().hex
            self.sessions[uid] = {'id': uid, 'store': store, 'account': self.account_key(store),
                'set': item, 'plan': plan, 'fingerprint': fingerprint, 'running': True,
                'revision': revision,
                'created_at': self.clock(), 'next_at': self.clock(), 'future': None,
                'history': LiveHistory(), 'streams': DetectorStreams(detector),
                'error': None, 'progress': 'Ожидание запуска', 'failures': 0}
            self.active[key] = uid
        return True

    def stop(self, store, session_id):
        with self.lock:
            session = self._owned(store, session_id)
            session['running'] = False

    def stop_set(self, store, set_id):
        with self.lock:
            for session in self.sessions.values():
                if session['account'] == self.account_key(store) and session['set']['id'] == set_id:
                    session['running'] = False

    def _owned(self, store, session_id):
        session = self.sessions.get(session_id)
        if session is None or session['account'] != self.account_key(store):
            raise ValueError('Сессия не принадлежит этой учётке.')
        return session

    def summaries(self, store):
        with self.lock:
            return [{'id': s['id'], 'set_id': s['set']['id'], 'name': s['set']['name'],
                'start': s['set']['start'], 'end': s['set']['end'], 'refresh_seconds': s['set']['refresh_seconds'],
                'running': s['running'], 'in_flight': s['future'] is not None and not s['future'].done(),
                'runs': len(s['history'].runs), 'failures': s['failures'], 'error': s['error'],
                'progress': s['progress'], 'created_at': s['created_at'],
                'source_series': len(s['history'].sources), 'components': len(s['history'].components),
                'source_points': sum(len(v['points']) for v in s['history'].sources.values()),
                'reaction_points': s['history'].reaction_points,
                'evicted_runs': s['history'].evicted_runs,
                'last_run': ({k: v for k, v in s['history'].runs[-1].items() if k not in {'components', 'metadata'}}
                             if s['history'].runs else None)}
                for s in self.sessions.values() if s['account'] == self.account_key(store)]

    def component_options(self, store, session_id):
        with self.lock:
            return copy.deepcopy(list(self._owned(store, session_id)['history'].components.values()))

    def metadata(self, store, session_id, run=-1):
        with self.lock:
            session = self._owned(store, session_id)
            history = session['history']
            return copy.deepcopy(history.runs[run]['metadata']) if history.runs else None

    def delete(self, store, session_id):
        with self.lock:
            session = self._owned(store, session_id)
            if session['running'] or (session['future'] is not None and not session['future'].done()):
                raise ValueError('Сначала остановите сессию и дождитесь завершения расчёта.')
            del self.sessions[session_id]
            self.active = {k: v for k, v in self.active.items() if v != session_id}

    def snapshot(self, store, session_id, selected, run=None):
        with self.lock:
            return self._owned(store, session_id)['history'].snapshot(selected, run)

    def tick(self):
        """One scheduling pass, also usable with a controlled clock in tests."""
        with self.lock:
            ttl = limit('LIVE_RETENTION_SECONDS', 86400)
            for uid, session in list(self.sessions.items()):
                if (not session['running'] and self.clock() - session['created_at'] > ttl
                        and (session['future'] is None or session['future'].done())):
                    del self.sessions[uid]
                    self.active = {k: v for k, v in self.active.items() if v != uid}
            accounts = {s['account']: s['store'] for s in self.sessions.values() if s['running']}
        snapshots = {}
        for key, store in accounts.items():
            try:
                snapshots[key] = store.snapshot()
            except Exception:
                # Retry a transient configuration outage without exposing connection strings.
                with self.lock:
                    for session in self.sessions.values():
                        if session['account'] == key and session['running']:
                            session['error'] = 'Не удалось прочитать настройки. Повторим попытку.'
        with self.lock:
            if self.closed.is_set():
                return
            for session in self.sessions.values():
                if not session['running'] or session['account'] not in snapshots:
                    continue
                workspace, revision = snapshots[session['account']]
                if session['revision'] == revision:
                    continue
                try:
                    _, _, fingerprint = prepare_set(workspace, session['set']['id'])
                    unchanged = fingerprint == session['fingerprint']
                except (ValueError, TypeError, KeyError):
                    unchanged = False
                if not unchanged:
                    session.update(running=False, error='Настройки сета или его метрик изменились. Запустите новую сессию.')
                else:
                    session['revision'] = revision
            busy = sum(s['future'] is not None and not s['future'].done() for s in self.sessions.values())
            busy_sets = {(s['account'], s['set']['id']) for s in self.sessions.values()
                         if s['future'] is not None and not s['future'].done()}
            now = self.clock()
            due = sorted((s for s in self.sessions.values() if s['running'] and s['account'] in snapshots
                          and (s['account'], s['set']['id']) not in busy_sets
                          and s['next_at'] <= now and (s['future'] is None or s['future'].done())),
                         key=lambda s: s['next_at'])
            for session in due[:max(0, self.max_workers - busy)]:
                try:
                    bounds = window_bounds(session['set'], now)
                except (ValueError, OverflowError):
                    session.update(running=False, error='Текущее окно некорректно. Проверьте его начало и конец.')
                    continue
                session.update(next_at=now + session['set']['refresh_seconds'], progress='Чтение текущего окна')
                session['future'] = self.pool.submit(self._execute, session, bounds)

    def _execute(self, session, bounds):
        def reader(source):
            metrics = [m for m in session['plan']['metrics'] if m['datasource_id'] == source.id]
            raw, skipped = self.reader(source, *bounds, metrics)
            # Keep the window contract even when a source returns additional samples.
            limited = {}
            for uid, metric in raw.items():
                series = []
                for item in metric['series']:
                    pairs = [(t, v) for t, v in zip(item['times'], item['values']) if bounds[0] <= t < bounds[1]]
                    if pairs:
                        series.append({**item, 'times': [t for t, _ in pairs], 'values': [v for _, v in pairs]})
                if series:
                    limited[uid] = {**metric, 'series': series}
            return limited, skipped

        def progress(message):
            with self.lock:
                session['progress'] = message

        try:
            result = replay(WindowStore(session['plan']), progress, reader=reader, streams=session['streams'])
            result['session_id'] = session['id']
            result['configuration_fingerprint'] = session['fingerprint']
            with self.lock:
                session['history'].append(result, bounds)
                # Preserve a configuration-change notice if it occurred during this evaluation.
                if session['running']:
                    session['error'] = None
                session['progress'] = 'Окно рассчитано'
        except Exception:
            with self.lock:
                session['failures'] += 1
                session['error'] = 'Окно не рассчитано. Проверьте источник и параметры; следующий запуск повторит попытку.'

    def _loop(self):
        while not self.closed.is_set():
            try:
                self.tick()
            except Exception:
                # A bad window must not terminate scheduling for other accounts/sets.
                with self.lock:
                    for session in self.sessions.values():
                        if session['running']:
                            session['error'] = 'Не удалось запланировать окно. Проверьте настройки сета.'
            self.closed.wait(1)

    def shutdown(self):
        self.closed.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        self.pool.shutdown(wait=True, cancel_futures=True)


def display_time(value):
    return str(pd.to_datetime(value, unit='s', utc=True).tz_convert('Europe/Moscow'))
