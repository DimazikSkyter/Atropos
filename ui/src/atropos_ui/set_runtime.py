"""Account-owned, persisted set evaluations from source windows or saved samples."""
import copy
import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from threading import RLock

from atropos_ui.clients import ApiError
from atropos_ui.detector_runtime import replay
from atropos_ui.live_runtime import WindowStore, prepare_set
from atropos_ui.metric_reader import read_metric_window
from atropos_ui.resource_limits import limit


def input_signatures(plan):
    sources = {s['id']: s for s in plan['sources']}
    return {m['id']: hashlib.sha256(json.dumps({
        'source': sources[m['datasource_id']],
        'metric': {k: m[k] for k in ('kind', 'query', 'measurement', 'field') if k in m},
    }, sort_keys=True).encode()).hexdigest() for m in plan['metrics']}


class SetRuntime:
    def __init__(self, *, reader=read_metric_window):
        self.reader = reader
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='atropos-set')
        self.lock = RLock()
        self.jobs = {}

    @staticmethod
    def key(store, set_id):
        return store._dsn, store.username, set_id

    def start(self, store, set_id, start_ns, end_ns, step, mode='source'):
        if mode not in {'source', 'saved'}:
            raise ValueError('Выберите источник данных или сохранённые серии.')
        if (type(start_ns) is not int or type(end_ns) is not int or end_ns <= start_ns
                or type(step) not in (int, float) or not math.isfinite(step) or step <= 0):
            raise ValueError('Некорректное окно или шаг расчёта.')
        workspace, revision = store.snapshot()
        item, plan, fingerprint = prepare_set(workspace, set_id)
        saved = store.load_set_result(set_id) if mode == 'saved' else None
        if mode == 'saved':
            if saved is None:
                raise ValueError('Сначала рассчитайте сет из источников: сохранённых серий ещё нет.')
            signatures = saved.get('input_signatures', {})
            if any(signatures.get(uid) != value for uid, value in input_signatures(plan).items()):
                raise ValueError('Состав или запросы метрик изменились. Загрузите окно из источников заново.')
            start_ns, end_ns = saved['bounds_ns']
            step = saved['step']
        key = self.key(store, set_id)
        with self.lock:
            now = time.time()
            self.jobs = {k: job for k, job in self.jobs.items()
                         if not job['future'].done() or now - job['started_at'] < 3600}
            previous = self.jobs.get(key)
            if previous and not previous['future'].done():
                return False
            if len(self.jobs) >= limit('SET_MAX_JOBS', 64) and key not in self.jobs:
                raise ValueError('Очередь расчётов заполнена. Повторите запуск позже.')
            state = {'running': True, 'error': None, 'progress': 'Запуск расчёта', 'started_at': now}

            def run():
                def reader(source):
                    metrics = [m for m in plan['metrics'] if m['datasource_id'] == source.id]
                    if saved is not None:
                        return {m['id']: copy.deepcopy(saved['metrics'][m['id']]) for m in metrics}, 0
                    return self.reader(source, start_ns, end_ns, metrics, step=step)

                try:
                    result = replay(WindowStore(plan), lambda message: state.update(progress=message), reader=reader)
                    errors = [m['name'] + ': ' + m['message'] for m in result['metrics'].values()
                              if m.get('status') == 'error']
                    if errors:
                        raise ValueError('; '.join(errors))
                    result.update(set_id=set_id, set_name=item['name'], revision=revision,
                                  bounds_ns=[start_ns, end_ns], step=step,
                                  configuration_fingerprint=fingerprint, input_signatures=input_signatures(plan),
                                  execution_mode='saved_series' if saved is not None else 'source_window')
                    store.save_set_result(set_id, result, expected_revision=revision)
                    state.update(progress='Расчёт сохранён в PostgreSQL')
                except (ValueError, ApiError) as exc:
                    state['error'] = str(exc)
                except Exception:
                    state['error'] = 'Расчёт не завершён. Проверьте подключение и повторите запуск.'
                finally:
                    state['running'] = False

            state['future'] = self.pool.submit(run)
            self.jobs[key] = state
        return True

    def state(self, store, set_id):
        prepare_set(store.snapshot()[0], set_id)
        with self.lock:
            job = self.jobs.get(self.key(store, set_id))
            return {k: v for k, v in job.items() if k != 'future'} if job else None

    def result(self, store, set_id):
        prepare_set(store.snapshot()[0], set_id)
        return store.load_set_result(set_id)

    def shutdown(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
