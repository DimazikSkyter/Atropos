"""Operator commands: migrations, legacy import, and persistent server settings."""
import argparse
import getpass
import json
import os
import sqlite3

from atropos_ui.service_api import ServiceSettings, read_legacy_configuration
from atropos_ui.storage import ConfigurationError, Store


def account_command(args):
    from atropos_ui.auth import password_hash
    from atropos_ui.detector_runtime import DETECTORS, replay
    from atropos_ui.influx import descriptors, read_source
    from atropos_ui.pages.detectors import suggested_peers
    from atropos_ui.service_api import AccountStore, DatasourceService, create_account
    if args.command == 'create-user':
        password = os.environ.get(args.password_env, '') if args.password_env else getpass.getpass('New account password: ')
        if len(password) < 12:
            raise ValueError('Пароль должен содержать минимум 12 символов.')
        if not create_account(args.username, password_hash(password)):
            raise ValueError('Учётная запись уже существует; её пароль и настройки не изменены.')
        print('Account created: ' + args.username)
        return
    store = AccountStore(args.username)
    store.snapshot()  # Fail before contacting Influx if the target account does not exist.
    if args.command == 'import-influx':
        service = DatasourceService(store)
        source = next((s for s in service.list() if s.name == args.source_name), None)
        if source is None:
            source = service.create(args.source_name, args.url, 'InfluxDB', influx_version='2',
                organization=args.org, bucket=args.bucket, auth_type='token', token_env=args.token_env,
                timeout_seconds=120)
        _, revision = store.snapshot()
        data, skipped = read_source(source)
        found = suggested_peers(descriptors(data, DETECTORS))
        store.import_metrics(found, expected_revision=revision)
        print(f'Imported for {args.username}: {len(found)} metrics, '
              f'{sum(len(m["series"]) for m in found)} series; skipped points: {skipped}')
        return
    result = replay(store, lambda message: print(message, flush=True))
    summary = {'account': args.username, 'metrics': len(result['metrics']),
               'series': sum(len(m['series']) for m in result['metrics'].values()),
               'points': sum(len(s['times']) for m in result['metrics'].values() for s in m['series']),
               'runs': [{'metric': m['name'], 'detector': name, 'status': d['status'],
                         'message': d.get('message', ''), 'series': len(d.get('series', []))}
                        for m in result['metrics'].values() for name, d in m['detectors'].items()],
               'metric_errors': [m['name'] + ': ' + m['message'] for m in result['metrics'].values() if m['status'] == 'error'],
               'warnings': result['warnings'], 'elapsed_seconds': result['finished_at'] - result['started_at']}
    if args.report:
        from pathlib import Path
        Path(args.report).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary['metric_errors'] or any(r['status'] == 'error' for r in summary['runs']):
        raise ValueError('Прогон завершился с ошибками; см. отчёт.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Atropos PostgreSQL configuration')
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('migrate', help='Apply versioned PostgreSQL schema migrations')
    legacy = subparsers.add_parser('import-sqlite', help='Import an old SQLite config without modifying it')
    legacy.add_argument('path')
    settings = subparsers.add_parser('settings', help='Read or change persistent service settings')
    settings.add_argument('--server-url')
    settings.add_argument('--server-token-env')
    create = subparsers.add_parser('create-user', help='Create a local account without inheriting another catalog')
    create.add_argument('--username', required=True)
    create.add_argument('--password-env', help='Environment variable containing the new password; otherwise prompt')
    importer = subparsers.add_parser('import-influx', help='Import all numeric history into one account')
    importer.add_argument('--username', required=True)
    importer.add_argument('--source-name', default='InfluxDB Replay')
    importer.add_argument('--url', default='http://127.0.0.1:8086')
    importer.add_argument('--org', default='atropos')
    importer.add_argument('--bucket', default='metrics-replay')
    importer.add_argument('--token-env', default='INFLUXDB_TOKEN')
    runner = subparsers.add_parser('replay', help='Rebuild one account from original data')
    runner.add_argument('--username', required=True)
    runner.add_argument('--report')
    args = parser.parse_args(argv)
    try:
        store = Store()
        if args.command in {'create-user', 'import-influx', 'replay'}:
            account_command(args)
        elif args.command == 'migrate':
            store.migrate()
            print('PostgreSQL configuration schema is ready.')
        elif args.command == 'import-sqlite':
            counts = store.import_configuration(*read_legacy_configuration(args.path))
            print(f'Imported {counts[0]} datasource(s), {counts[1]} definition(s). SQLite was not modified.')
        else:
            current = store.settings()
            if args.server_url is not None or args.server_token_env is not None:
                current = ServiceSettings(
                    args.server_url if args.server_url is not None else current.server_url,
                    args.server_token_env if args.server_token_env is not None else current.server_token_env,
                )
                store.save_settings(current)
            print(f'Server URL: {current.server_url}; token environment variable: {current.server_token_env or "(none)"}')
    except (ConfigurationError, ValueError, TypeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'Configuration error: {exc}\n')


if __name__ == '__main__':
    main()
