"""Private, bounded HTTP gateway for one process owning replay and live jobs.

The UI authenticates humans; this internal API trusts only the UI service token.
AccountStore still checks enabled accounts and LiveRuntime checks session ownership.
It is separate from the proposed derived-PromQL Metrics Server contract.
"""
import argparse
import hmac
import json
import logging
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore, Thread
from urllib.parse import urlsplit

from atropos_ui.detector_runtime import ReplayRuntime
from atropos_ui.live_runtime import LiveRuntime
from atropos_ui.resource_limits import limit
from atropos_ui.runtime_client import runtime_token
from atropos_ui.service_api import (
    AccountStore,
    ConfigurationError,
    PostgresConfigRepository,
    startup_accounts,
)
from atropos_ui.set_runtime import SetRuntime

LOG = logging.getLogger('atropos.runtime')
OPERATIONS = {
    'sets': {'start': {'set_id', 'start_ns', 'end_ns', 'step', 'mode'},
             'state': {'set_id'}, 'result': {'set_id'}},
    'replay': {'start': set(), 'state': set(), 'result': set()},
    'live': {'start': {'set_id'}, 'stop': {'session_id'}, 'stop_set': {'set_id'},
             'delete': {'session_id'}, 'summaries': set(), 'component_options': {'session_id'},
             'metadata': {'session_id', 'run'}, 'snapshot': {'session_id', 'selected', 'run'}},
}


class RuntimeService:
    def __init__(self, *, replay=None, live=None, sets=None, store_factory=AccountStore, health_check=None):
        self.replay = replay if replay is not None else ReplayRuntime()
        self.live = live if live is not None else LiveRuntime()
        self.sets = sets if sets is not None else SetRuntime()
        self.store_factory = store_factory
        self.health_check = health_check or (lambda: PostgresConfigRepository().settings())

    def startup(self):
        for username in startup_accounts():
            store = self.store_factory(username)
            if store.metrics() and not store.metric_sets():
                try:
                    self.replay.start(store)
                except ValueError:
                    LOG.warning('Startup replay queue is full; remaining accounts require explicit replay')
                    break

    def execute(self, scope, action, account, arguments):
        if scope not in OPERATIONS or action not in OPERATIONS[scope]:
            raise KeyError('Unknown operation')
        if (not isinstance(account, str) or not account or len(account) > 80
                or not isinstance(arguments, dict) or set(arguments) != OPERATIONS[scope][action]):
            raise ValueError('Некорректная учётка или аргументы операции.')
        for key in ('set_id', 'session_id'):
            if key in arguments and (not isinstance(arguments[key], str) or not arguments[key] or len(arguments[key]) > 160):
                raise ValueError('Некорректный ID.')
        if 'run' in arguments and arguments['run'] is not None and type(arguments['run']) is not int:
            raise ValueError('Номер окна должен быть целым числом.')
        if action == 'snapshot':
            selected = arguments['selected']
            if (not isinstance(selected, list) or len(selected) > 20
                    or any(not isinstance(s, str) or len(s) > 160 for s in selected)):
                raise ValueError('Выберите не больше 20 реакций.')
        store = self.store_factory(account)
        store.snapshot()  # Every request rechecks that the account is still enabled.
        target = {'replay': self.replay, 'live': self.live, 'sets': self.sets}[scope]
        result = getattr(target, action)(store, **arguments)
        if scope == 'replay' and action == 'state' and result is not None:
            state = {k: v for k, v in result.items() if k not in {'future', 'result'}}
            state['running'] = not result['future'].done()
            saved = result['result']
            state['result'] = ({k: saved[k] for k in ('revision', 'finished_at') if k in saved}
                               if saved is not None else None)
            return state
        return result

    def shutdown(self):
        self.sets.shutdown()
        self.live.shutdown()
        self.replay.pool.shutdown(wait=True, cancel_futures=True)


class RuntimeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service, token):
        self.service, self.token = service, token
        self.slots = BoundedSemaphore(limit('RUNTIME_HTTP_WORKERS', 32))
        super().__init__(address, RuntimeHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class RuntimeHandler(BaseHTTPRequestHandler):
    server_version = 'AtroposRuntime/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass  # No usernames, request payloads, tokens or source details in access logs.

    def respond(self, status, data):
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > limit('HTTP_MAX_BYTES', 64 * 1024 * 1024):
            status, encoded = 413, b'{"error":"Runtime response exceeds ATROPOS_HTTP_MAX_BYTES"}'
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/healthz':
            try:
                self.server.service.health_check()
                self.respond(200, {'status': 'ok', 'role': 'atropos-runtime'})
            except Exception:
                self.respond(503, {'status': 'unavailable'})
        elif path == '/metrics':
            content = b'# HELP atropos_runtime_up Runtime HTTP process is available.\n# TYPE atropos_runtime_up gauge\natropos_runtime_up 1\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.respond(404, {'error': 'Unknown endpoint'})

    def do_POST(self):
        expected = ('Bearer ' + self.server.token).encode()
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), expected):
            self.respond(401, {'error': 'Runtime service authentication required'})
            return
        parts = urlsplit(self.path).path.strip('/').split('/')
        if len(parts) != 5 or parts[:3] != ['api', 'v1', 'runtime']:
            self.respond(404, {'error': 'Unknown endpoint'})
            return
        try:
            length = int(self.headers.get('Content-Length', '-1'))
            if self.headers.get('Transfer-Encoding') or not 0 <= length <= 65536:
                self.respond(413, {'error': 'Request body must be at most 65536 bytes'})
                return
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or set(body) != {'account', 'arguments'}:
                raise ValueError('Некорректный формат запроса.')
            data = self.server.service.execute(parts[3], parts[4], body['account'], body['arguments'])
            self.respond(200, {'data': data})
        except KeyError:
            self.respond(404, {'error': 'Unknown operation'})
        except (ValueError, TypeError, IndexError):
            self.respond(400, {'error': 'Проверьте учётку, права на сессию и аргументы операции.'})
        except ConfigurationError:
            self.respond(503, {'error': 'Configuration database unavailable'})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            LOG.error('Runtime operation failed; private details suppressed')
            self.respond(500, {'error': 'Runtime operation failed'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    token = runtime_token()
    service = RuntimeService()
    server = None
    try:
        service.health_check()
        service.startup()
        server = RuntimeHTTPServer((args.host, args.port), service, token)
        def stop(signum, frame):
            Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        LOG.info('Atropos runtime ready on port %s', args.port)
        server.serve_forever(poll_interval=0.5)
    finally:
        if server is not None:
            server.server_close()
        service.shutdown()


if __name__ == '__main__':
    main()
