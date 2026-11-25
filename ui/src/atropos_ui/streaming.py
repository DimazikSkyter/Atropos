"""Session-owned detector streams with bounded overlap caches and exact time alignment."""
import json
from collections import OrderedDict

from atropos_ui.resource_limits import limit


class DetectorStreams:
    def __init__(self, factory, *, max_points=None, max_streams=None):
        self.factory = factory
        self.max_points = max_points or limit('LIVE_STREAM_CACHE_POINTS', 250_000)
        self.max_streams = max_streams or limit('LIVE_MAX_STREAMS', 10_000)
        self.states = {}
        self.order = OrderedDict()
        self.model_points = 0

    def calculate(self, name, parameters, identities, times, values):
        if len(times) != len(values) or times != sorted(set(times)):
            raise ValueError('Временная ось должна быть строго возрастающей и соответствовать значениям.')
        key = (name, json.dumps(parameters, sort_keys=True), tuple(identities))
        if key not in self.states:
            if len(self.states) >= self.max_streams:
                raise ValueError('Достигнут лимит ATROPOS_LIVE_MAX_STREAMS. Создайте меньший сет.')
            instance = self.factory(name, parameters)
            config = getattr(instance, 'config', instance)
            sizes = [getattr(config, k, 0) or 0 for k in ('window', 'data_max_size', 'reference_window',
                                                          'inertia_window', 'persistence', 'max_samples')]
            cost = 4 * sum(sizes)
            if name == 'SSA':
                lag = instance.lag or instance.window // 2
                cost += instance.reference_window * lag + lag * lag
            if self.model_points + cost > limit('LIVE_MODEL_POINTS', 2_000_000):
                raise ValueError('Параметры превышают бюджет ATROPOS_LIVE_MODEL_POINTS. Уменьшите окна или сет.')
            self.model_points += cost
            self.states[key] = {'detector': instance, 'records': {},
                                'positions': {}, 'index': -1, 'last': None, 'discarded_before': None}
        state = self.states[key]
        records, instance = state['records'], state['detector']
        # Reject corrections/late arrivals before changing the detector.
        for stamp, value in zip(times, values):
            if stamp in records and records[stamp]['value'] != value:
                raise ValueError('Исходная точка изменилась. Запустите новую сессию для повторного обучения.')
            if (stamp not in records and state['last'] is not None and stamp <= state['last']
                    and (state['discarded_before'] is None or stamp > state['discarded_before'])):
                raise ValueError('Получена запоздалая точка. Запустите новую сессию для повторного обучения.')
        updated = set()
        for stamp, value in zip(times, values):
            if state['last'] is not None and stamp <= state['last']:
                continue
            if name == 'RuntimeTrend':
                instance.update_ns(stamp, value)
                result = instance.evaluate()
                score = float(result.detected)
            else:
                result = instance.update(value)
                score = float(result.score if hasattr(result, 'score') else result)
            state['index'] += 1
            state['last'] = stamp
            records[stamp] = {'value': value, 'score': None, 'index': state['index']}
            state['positions'][state['index']] = stamp
            self.order[key, stamp] = None
            position = getattr(result, 'score_index', None)
            target = state['positions'].get(state['index'] if position is None else position)
            ready = getattr(result, 'ready', getattr(instance, 'ready', False))
            if ready and target in records:
                records[target]['score'] = score
                updated.add(target)
            while len(self.order) > self.max_points:
                (old_key, old_stamp), _ = self.order.popitem(last=False)
                old_state = self.states[old_key]
                old = old_state['records'].pop(old_stamp)
                old_state['positions'].pop(old['index'], None)
                old_state['discarded_before'] = max(old_stamp, old_state['discarded_before'] or old_stamp)
        # Include delayed Correlation results even if their target left the visible window.
        output_times = sorted(set(times) | updated) if name == 'Correlation' else times
        scores = [records.get(t, {}).get('score') for t in output_times]
        count = sum(score is not None for score in scores)
        return {'times': output_times, 'scores': scores, 'ready_points': count,
                'status': 'ok' if count else 'warmup'}
