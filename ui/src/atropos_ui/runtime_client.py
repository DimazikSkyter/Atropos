"""Account-bound UI clients for the private detector runtime API."""
import os
from pathlib import Path

from atropos_ui.clients import ApiError, HttpApi


def runtime_token():
    path = os.getenv('ATROPOS_RUNTIME_TOKEN_FILE')
    token = Path(path).read_text(encoding='utf-8').strip() if path else os.getenv('ATROPOS_RUNTIME_TOKEN', '')
    if len(token) < 32:
        raise ValueError('Настройте runtime token: ATROPOS_RUNTIME_TOKEN_FILE или ATROPOS_RUNTIME_TOKEN (32+ символа).')
    return token


class JobStatus:
    def __init__(self, running):
        self.running = running

    def done(self):
        return not self.running


class RuntimeClient:
    scope = ''

    def __init__(self, url=None, *, transport=None):
        self.api = HttpApi(url or os.environ['ATROPOS_RUNTIME_URL'], transport=transport)

    def call(self, store, action, **arguments):
        payload = self.api.request('POST', f'/api/v1/runtime/{self.scope}/{action}',
            headers={'Authorization': 'Bearer ' + runtime_token()},
            json={'account': store.username, 'arguments': arguments})
        if not isinstance(payload, dict) or 'data' not in payload:
            raise ApiError('Некорректный ответ сервера рантайма.')
        return payload['data']


class RemoteReplayRuntime(RuntimeClient):
    scope = 'replay'

    def start(self, store):
        return self.call(store, 'start')

    def state(self, store):
        state = self.call(store, 'state')
        if state is not None:
            state['future'] = JobStatus(state.pop('running'))
        return state

    def result(self, store):
        return self.call(store, 'result')


class RemoteLiveRuntime(RuntimeClient):
    scope = 'live'

    def start(self, store, set_id):
        return self.call(store, 'start', set_id=set_id)

    def stop(self, store, session_id):
        return self.call(store, 'stop', session_id=session_id)

    def stop_set(self, store, set_id):
        return self.call(store, 'stop_set', set_id=set_id)

    def delete(self, store, session_id):
        return self.call(store, 'delete', session_id=session_id)

    def summaries(self, store):
        return self.call(store, 'summaries')

    def component_options(self, store, session_id):
        return self.call(store, 'component_options', session_id=session_id)

    def metadata(self, store, session_id, run=-1):
        return self.call(store, 'metadata', session_id=session_id, run=run)

    def snapshot(self, store, session_id, selected, run=None):
        return self.call(store, 'snapshot', session_id=session_id, selected=selected, run=run)


class RemoteSetRuntime(RuntimeClient):
    scope = 'sets'

    def start(self, store, set_id, start_ns, end_ns, step, mode='source'):
        return self.call(store, 'start', set_id=set_id, start_ns=start_ns, end_ns=end_ns, step=step, mode=mode)

    def state(self, store, set_id):
        return self.call(store, 'state', set_id=set_id)

    def result(self, store, set_id):
        return self.call(store, 'result', set_id=set_id)
