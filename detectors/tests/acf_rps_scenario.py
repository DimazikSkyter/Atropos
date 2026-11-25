"""Read-only InfluxDB RPS snapshot and its offline, checksum-verified fixture."""

import argparse
import base64
import csv
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import urllib.parse

import numpy as np

from tests.acf_replay_scenarios import ReplayCase


FIXTURES = Path(__file__).with_name('fixtures')
STEM = 'acf-rps-influx-15s'
UID = 'cusum-rps-20260922'
START = '2026-09-22T09:59:30Z'
STOP = '2026-09-23T08:58:32Z'
REPLAY = 'cusum-real-system-image'


def parse_points(raw):
    header, points = None, []
    for fields in csv.reader(io.StringIO(raw)):
        if not fields or fields[0].startswith('#'):
            continue
        if '_time' in fields and '_value' in fields:
            header = fields
            continue
        if header is None:
            raise ValueError('Flux data before header')
        row = dict(zip(header, fields))
        if row.get('_time'):
            stamp = datetime.fromisoformat(row['_time'].replace('Z', '+00:00'))
            value = float(row['_value'])
            if not math.isfinite(value):
                raise ValueError('RPS values must be finite')
            points.append((stamp.astimezone(timezone.utc), value))
    points.sort(key=lambda p: p[0])
    if not points or any((b[0]-a[0]).total_seconds() != 15
                         for a, b in zip(points, points[1:])):
        raise ValueError('Expected one nonempty, unique, uniform 15-second series')
    return points


def fetch_snapshot(fixtures=FIXTURES, infra_root=None):
    """Query only; never seed data or modify Grafana, InfluxDB or Docker."""
    settings = {}
    if infra_root is not None and (Path(infra_root)/'.env').is_file():
        for line in (Path(infra_root)/'.env').read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                settings[key.strip()] = value.strip().strip('\"\'')
    def setting(name, default):
        return os.getenv(name, settings.get(name, default))
    influx = setting('INFLUX_URL', setting('INFLUX_HOST',
                     'http://localhost:'+setting('INFLUXDB_PORT', '8086'))).rstrip('/')
    grafana = setting('GRAFANA_URL', 'http://localhost:'+setting('GRAFANA_PORT', '3001')).rstrip('/')
    org = setting('INFLUXDB_ORG', 'atropos')
    bucket = setting('INFLUXDB_BUCKET', 'metrics-replay')
    token = setting('INFLUXDB_TOKEN', 'atropos-local-replay-token')
    authorization = 'Basic '+base64.b64encode((setting('GRAFANA_ADMIN_USER', 'admin')+':'+
                                             setting('GRAFANA_ADMIN_PASSWORD', 'admin')).encode()).decode()
    def request(url, data=None, headers=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Fixture reader requires a local HTTP endpoint')
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
        try:
            connection.request('POST' if data is not None else 'GET',
                               parsed.path+('?' + parsed.query if parsed.query else ''),
                               body=data, headers=headers or {})
            response = connection.getresponse()
            text = response.read().decode('utf-8')
            if response.status != 200:
                raise RuntimeError(f'Read request failed: HTTP {response.status} at {parsed.path}')
            return text
        finally:
            connection.close()
    dashboard = json.loads(request(grafana+'/api/dashboards/uid/'+UID,
                                   headers={'Authorization': authorization}))['dashboard']
    panels = [p for p in dashboard['panels'] if p.get('targets')]
    matches = [(p, t['query']) for p in panels for t in p['targets']
               if REPLAY in t.get('query', '') and '"rps"' in t['query']]
    if len(matches) != 1:
        raise ValueError('Inspect changed dashboard: expected exactly one matching RPS target')
    panel, template = matches[0]
    if 'aggregateWindow' in template:
        raise ValueError('Expected raw RPS query without aggregation')
    query = (template.replace('v.defaultBucket', json.dumps(bucket))
             .replace('v.timeRangeStart', START).replace('v.timeRangeStop', STOP))
    raw = request(influx+'/api/v2/query?'+urllib.parse.urlencode({'org': org}), query.encode(),
                  {'Authorization': 'Token '+token, 'Content-Type': 'application/vnd.flux',
                   'Accept': 'application/csv'})
    points = parse_points(raw)
    body = {'from': str(int(datetime.fromisoformat(START.replace('Z', '+00:00')).timestamp()*1000)),
            'to': str(int(datetime.fromisoformat(STOP.replace('Z', '+00:00')).timestamp()*1000)),
            'queries': [{'refId': 'A', 'datasource': panel['datasource'], 'query': query,
                         'intervalMs': 15000, 'maxDataPoints': 10000}]}
    response = json.loads(request(grafana+'/api/ds/query', json.dumps(body).encode(),
                                  {'Authorization': authorization, 'Content-Type': 'application/json'}))
    answer = response['results']['A']
    if answer.get('error'):
        raise ValueError('Grafana datasource returned a query error')
    observed = []
    for frame in answer['frames']:
        fields, data = frame['schema']['fields'], frame['data']['values']
        time_index = next(i for i, f in enumerate(fields) if f['type'] == 'time')
        for i, field in enumerate(fields):
            if field['type'] == 'number':
                observed.extend((int(t), float(v)) for t, v in zip(data[time_index], data[i]))
    expected = [(int(t.timestamp()*1000), v) for t, v in points]
    if sorted(observed) != expected:
        raise ValueError('Grafana frames differ from direct InfluxDB snapshot')
    fixtures = Path(fixtures)
    fixtures.mkdir(parents=True, exist_ok=True)
    path = fixtures/(STEM+'.csv')
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(('timestamp_utc', 'value_rps'))
        writer.writerows((t.isoformat().replace('+00:00', 'Z'), repr(v)) for t, v in points)
    (fixtures/(STEM+'-raw.csv')).write_text(raw, encoding='utf-8')
    (fixtures/(STEM+'-query.flux')).write_text(query, encoding='utf-8')
    (fixtures/(STEM+'-dashboard.json')).write_text(json.dumps(dashboard, ensure_ascii=False, indent=2), encoding='utf-8')
    metadata = {'snapshot_date': datetime.now(timezone.utc).date().isoformat(),
                'measurement': 'rps', 'field': 'value', 'tags': {'replay': REPLAY},
                'influx_url': influx, 'org': org, 'bucket': bucket, 'dashboard_uid': UID,
                'requested_start_utc': START, 'requested_stop_utc': STOP,
                'actual_start_utc': points[0][0].isoformat().replace('+00:00', 'Z'),
                'actual_end_utc': points[-1][0].isoformat().replace('+00:00', 'Z'),
                'points': len(points), 'sample_interval_seconds': 15,
                'min_rps': min(v for _, v in points), 'max_rps': max(v for _, v in points),
                'csv_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'values_sha256': hashlib.sha256(np.asarray([v for _, v in points], dtype='<f8').tobytes()).hexdigest(),
                'grafana_frames_equal_influx': True, 'read_only': True,
                'source_kind': 'Live InfluxDB snapshot of a previously image-reconstructed RPS replay',
                'is_raw_production_telemetry': False}
    (fixtures/(STEM+'.json')).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    return metadata


def load_snapshot(fixtures=FIXTURES):
    fixtures = Path(fixtures)
    metadata = json.loads((fixtures/(STEM+'.json')).read_text(encoding='utf-8'))
    path = fixtures/(STEM+'.csv')
    if hashlib.sha256(path.read_bytes()).hexdigest() != metadata['csv_sha256']:
        raise ValueError('RPS snapshot checksum mismatch')
    with path.open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    times = tuple(datetime.fromisoformat(r['timestamp_utc'].replace('Z', '+00:00')) for r in rows)
    values = np.asarray([float(r['value_rps']) for r in rows])
    if len(values) != metadata['points'] or not np.isfinite(values).all():
        raise ValueError('RPS snapshot length or values are invalid')
    if any((b-a).total_seconds() != 15 for a, b in zip(times, times[1:])):
        raise ValueError('RPS snapshot requires the original 15-second grid')
    if hashlib.sha256(values.astype('<f8').tobytes()).hexdigest() != metadata['values_sha256']:
        raise ValueError('RPS value fingerprint mismatch')
    return ReplayCase('influx-rps-15s', 'RPS из InfluxDB: краткая просадка и длительное плато',
                      values, times, metadata['source_kind'], zoom_index=int(np.argmin(values))), metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--infra-root', type=Path)
    args = parser.parse_args()
    if not args.fetch:
        parser.error('Use --fetch to explicitly refresh the snapshot')
    print(json.dumps(fetch_snapshot(infra_root=args.infra_root), ensure_ascii=False, indent=2))
