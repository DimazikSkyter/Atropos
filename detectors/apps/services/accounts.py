"""Account-bound configuration. No implicit access to the legacy shared catalog.

All reads and writes use the server-authenticated username. A workspace is changed
under a row lock; a replay can only publish against the revision it computed.
"""
import gzip
import json
from dataclasses import asdict

from psycopg.types.json import Jsonb

from .configuration import MemoryConfigRepository, MetricDefinition, ServiceSettings
from .datasources import Datasource
from .metric_sets import catalog_metrics, definition_id, validate_metric_set
from .metric_validation import validate_metric
from .postgres import PostgresConfigRepository

DEFAULT_PREFERENCES = {
    'persist_results': False, 'rebuild_on_login': True, 'rebuild_on_startup': True,
}


def create_account(username, encoded, *, dsn=None, legacy=False):
    if not isinstance(username, str) or not username.strip() or len(username) > 80:
        raise ValueError('Логин должен содержать от 1 до 80 символов.')
    if not encoded.startswith('pbkdf2_sha256$'):
        raise ValueError('Требуется хеш пароля PBKDF2.')
    base = PostgresConfigRepository(dsn)
    workspace = {}
    if legacy:
        workspace = {'sources': [asdict(s) for s in base.sources()],
                     'definitions': [asdict(d) for d in base.definitions()],
                     'settings': asdict(base.settings())}
    with base._connection() as db:
        # Never reset passwords or overwrite an existing user's configuration.
        return db.execute('''INSERT INTO atropos_config.accounts(username, password_hash, workspace)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING username''',
                          (username, encoded, Jsonb(workspace))).fetchone() is not None


def account_hash(username, dsn=None):
    with PostgresConfigRepository(dsn)._connection() as db:
        row = db.execute('SELECT password_hash FROM atropos_config.accounts WHERE username = %s AND enabled',
                         (username,)).fetchone()
        return row['password_hash'] if row else None


def startup_accounts(dsn=None):
    with PostgresConfigRepository(dsn)._connection() as db:
        return [r['username'] for r in db.execute('''SELECT username FROM atropos_config.accounts
            WHERE enabled AND COALESCE((workspace->'preferences'->>'rebuild_on_startup')::boolean, true)
            ORDER BY username''')]


class AccountStore(PostgresConfigRepository):
    def __init__(self, username, dsn=None):
        super().__init__(dsn)
        if not isinstance(username, str) or not username:
            raise ValueError('Требуется авторизованная учётная запись.')
        self.username = username

    def snapshot(self):
        with self._connection() as db:
            row = db.execute('SELECT workspace, revision FROM atropos_config.accounts WHERE username = %s AND enabled',
                             (self.username,)).fetchone()
            if row is None:
                raise ValueError('Учётная запись недоступна.')
            return row['workspace'], row['revision']

    def _edit(self, change, *, expected_revision=None):
        with self._connection() as db:
            row = db.execute('''SELECT workspace, revision FROM atropos_config.accounts
                WHERE username = %s AND enabled FOR UPDATE''', (self.username,)).fetchone()
            if row is None:
                raise ValueError('Учётная запись недоступна.')
            if expected_revision is not None and row['revision'] != expected_revision:
                raise ValueError('Настройки изменились. Обновите страницу и повторите действие.')
            workspace = row['workspace']
            result = change(workspace)
            db.execute('''UPDATE atropos_config.accounts SET workspace = %s, revision = revision + 1,
                updated_at = now() WHERE username = %s''', (Jsonb(workspace), self.username))
            db.execute('DELETE FROM atropos_config.account_results WHERE username = %s', (self.username,))
            return result

    @staticmethod
    def _config(workspace):
        repo = MemoryConfigRepository()
        repo.edit_sources(lambda _: [Datasource(**s) for s in workspace.get('sources', [])])
        for item in workspace.get('definitions', []):
            repo.save(MetricDefinition(**item))
        repo.save_settings(ServiceSettings(**workspace.get('settings', {})))
        return repo

    def _config_edit(self, operation, *, rename=None):
        def change(workspace):
            repo = self._config(workspace)
            result = operation(repo)
            ids = {s.id for s in repo.sources()}
            if any(m['datasource_id'] not in ids for m in workspace.get('metrics', [])):
                raise ValueError('Источник используется импортированными метриками этой учётки.')
            by_id = {s.id: s for s in repo.sources()}
            if any(by_id[m['datasource_id']].kind != 'InfluxDB' or by_id[m['datasource_id']].influx_version != '2'
                   for m in workspace.get('metrics', [])):
                raise ValueError('Источник используется импортированными метриками InfluxDB 2.x.')
            workspace.update(sources=[asdict(s) for s in repo.sources()],
                             definitions=[asdict(d) for d in repo.definitions()], settings=asdict(repo.settings()))
            if rename and rename[0] != rename[1]:
                old, new = (definition_id(name) for name in rename)
                for item in workspace.get('metric_sets', []):
                    for member in item['members']:
                        if member['metric_id'] == old:
                            member['metric_id'] = new
                        member['peers'] = [new if uid == old else uid for uid in member['peers']]
            for item in workspace.get('metric_sets', []):
                try:
                    validate_metric_set(item, catalog_metrics(workspace))
                except ValueError as exc:
                    raise ValueError('Метрика используется в сете. Сначала измените состав сета. ' + str(exc)) from exc
            return result
        return self._edit(change)

    def sources(self):
        return self._config(self.snapshot()[0]).sources()

    def definitions(self):
        return self._config(self.snapshot()[0]).definitions()

    def settings(self):
        return self._config(self.snapshot()[0]).settings()

    def edit_sources(self, change):
        return self._config_edit(lambda repo: repo.edit_sources(change))

    def save(self, item):
        return self._config_edit(lambda repo: repo.save(item))

    def save_definition(self, item, *, previous_name=None):
        return self._config_edit(lambda repo: repo.save_definition(item, previous_name=previous_name),
                                 rename=(previous_name, item.name) if previous_name else None)

    def delete(self, kind, key):
        return self._config_edit(lambda repo: repo.delete(kind, key))

    def save_settings(self, settings):
        return self._config_edit(lambda repo: repo.save_settings(settings))

    def preferences(self):
        return {**DEFAULT_PREFERENCES, **self.snapshot()[0].get('preferences', {})}

    def save_preferences(self, preferences, *, expected_revision=None):
        if set(preferences) != set(DEFAULT_PREFERENCES) or any(type(v) is not bool for v in preferences.values()):
            raise ValueError('Некорректные настройки учётки.')
        self._edit(lambda w: w.update(preferences=preferences), expected_revision=expected_revision)

    def metrics(self):
        return self.snapshot()[0].get('metrics', [])

    def metric_sets(self):
        return self.snapshot()[0].get('metric_sets', [])

    def catalog_metrics(self):
        return catalog_metrics(self.snapshot()[0])

    def save_metric_set(self, item, *, expected_revision):
        def change(workspace):
            validated = validate_metric_set(item, catalog_metrics(workspace))
            sets = {s['id']: s for s in workspace.get('metric_sets', [])}
            sets[validated['id']] = validated
            workspace['metric_sets'] = list(sets.values())
        self._edit(change, expected_revision=expected_revision)

    def remove_metric_set(self, set_id, *, expected_revision):
        def change(workspace):
            workspace['metric_sets'] = [s for s in workspace.get('metric_sets', []) if s['id'] != set_id]
        self._edit(change, expected_revision=expected_revision)

    def save_set_result(self, set_id, result, *, expected_revision):
        payload = gzip.compress(json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
        if len(payload) > 32 * 1024 * 1024:
            raise ValueError('Результат слишком велик для сохранения. Сократите окно или сет.')
        with self._connection() as db:
            row = db.execute('SELECT workspace, revision FROM atropos_config.accounts '
                             'WHERE username = %s AND enabled FOR UPDATE', (self.username,)).fetchone()
            if row is None or row['revision'] != expected_revision:
                raise ValueError('Настройки изменились во время расчёта. Повторите запуск.')
            if not any(s['id'] == set_id for s in row['workspace'].get('metric_sets', [])):
                raise ValueError('Сет не принадлежит этой учётке.')
            db.execute('''INSERT INTO atropos_config.account_set_results(username, set_id, payload)
                VALUES (%s, %s, %s) ON CONFLICT (username, set_id) DO UPDATE
                SET payload = EXCLUDED.payload, updated_at = now()''', (self.username, set_id, payload))

    def load_set_result(self, set_id):
        with self._connection() as db:
            row = db.execute('''SELECT r.payload FROM atropos_config.account_set_results r
                JOIN atropos_config.accounts a USING (username) WHERE r.username = %s AND r.set_id = %s
                AND a.enabled AND EXISTS (SELECT 1 FROM jsonb_array_elements(
                    COALESCE(a.workspace->'metric_sets', '[]'::jsonb)) s WHERE s->>'id' = r.set_id)''',
                             (self.username, set_id)).fetchone()
            return json.loads(gzip.decompress(row['payload'])) if row else None

    def import_metrics(self, metrics, *, expected_revision):
        if not isinstance(metrics, list) or any(not isinstance(m, dict) for m in metrics):
            raise ValueError('Импорт метрик должен быть списком объектов.')
        ids = [m.get('id') for m in metrics]
        if any(not isinstance(uid, str) or not uid for uid in ids) or len(set(ids)) != len(ids):
            raise ValueError('ID импортированных метрик должны быть уникальными непустыми строками.')
        def change(workspace):
            sources = {s['id']: s for s in workspace.get('sources', [])}
            existing = {m['id']: m for m in workspace.get('metrics', [])}
            for metric in metrics:
                if any(not isinstance(metric.get(k), str) or not metric[k] for k in
                       ('id', 'datasource_id', 'measurement', 'field')):
                    raise ValueError('Некорректный указатель исходной метрики.')
                if metric['datasource_id'] not in sources:
                    raise ValueError('Источник не принадлежит этой учётке.')
                source = sources[metric['datasource_id']]
                if source['kind'] != 'InfluxDB' or source.get('influx_version', '2') != '2':
                    raise ValueError('Импорт метрик поддерживает только InfluxDB 2.x.')
                previous = existing.get(metric['id'], {})
                # Refresh source metadata; retain the user's names, labels and bindings.
                existing[metric['id']] = {**metric, **{k: previous[k] for k in
                    ('name', 'extra_labels', 'drop_labels', 'detectors', 'peers') if k in previous}}
            known = set(existing)
            workspace['metrics'] = [validate_metric(m, known) for m in existing.values()]
        self._edit(change, expected_revision=expected_revision)

    def update_metric(self, metric_id, changes, *, expected_revision):
        allowed = {'name', 'extra_labels', 'drop_labels', 'detectors', 'peers'}
        if set(changes) - allowed:
            raise ValueError('Нельзя менять указатель на исходную метрику.')
        def change(workspace):
            metrics = workspace.get('metrics', [])
            metric = next((m for m in metrics if m['id'] == metric_id), None)
            if metric is None:
                raise ValueError('Метрика не принадлежит этой учётке.')
            validated = validate_metric({**metric, **changes}, {m['id'] for m in metrics})
            metric.update(validated)
        self._edit(change, expected_revision=expected_revision)

    def remove_metric(self, metric_id, *, expected_revision):
        def change(workspace):
            if any(member['metric_id'] == metric_id or metric_id in member.get('peers', [])
                   for item in workspace.get('metric_sets', []) for member in item['members']):
                raise ValueError('Метрика используется в сете. Сначала измените или удалите сет.')
            workspace['metrics'] = [m for m in workspace.get('metrics', []) if m['id'] != metric_id]
            for metric in workspace['metrics']:
                metric['peers'] = [p for p in metric.get('peers', []) if p != metric_id]
        self._edit(change, expected_revision=expected_revision)

    def save_results(self, result, revision):
        payload = gzip.compress(json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
        with self._connection() as db:
            row = db.execute('''SELECT revision, workspace FROM atropos_config.accounts
                WHERE username = %s AND enabled FOR UPDATE''', (self.username,)).fetchone()
            if row is None or row['revision'] != revision:
                return False
            if not row['workspace'].get('preferences', {}).get('persist_results', False):
                return False
            db.execute('''INSERT INTO atropos_config.account_results(username, revision, payload) VALUES (%s,%s,%s)
                ON CONFLICT (username) DO UPDATE SET revision = EXCLUDED.revision, payload = EXCLUDED.payload,
                updated_at = now()''', (self.username, revision, payload))
            return True

    def load_results(self):
        with self._connection() as db:
            row = db.execute('''SELECT r.payload FROM atropos_config.account_results r
                JOIN atropos_config.accounts a USING (username) WHERE r.username = %s AND a.enabled
                AND r.revision = a.revision AND (a.workspace->'preferences'->>'persist_results')::boolean''',
                             (self.username,)).fetchone()
            return json.loads(gzip.decompress(row['payload'])) if row else None
