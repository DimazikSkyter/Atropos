"""Offline service-response-time snapshots; explicit refresh reads local APIs."""

import argparse
import base64
import csv
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import urllib.parse

import numpy as np

from tests.acf_replay_scenarios import ReplayCase
from tests.acf_rps_scenario import parse_points


FIXTURES = Path(__file__).with_name('fixtures')
STEM = 'acf-service-response-20261005'
UID = 'service-response-time-20261005'
START = '2026-10-04T21:00:00Z'
STOP = '2026-10-05T08:50:00Z'


def fetch_snapshot(fixtures=FIXTURES, infra_root=None):
    """Read panel 1, raw InfluxDB points and Grafana frames; never modify them."""
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
    grafana = setting('GRAFANA_URL', 'http://127.0.0.1:'+setting('GRAFANA_PORT', '3001')).rstrip('/')
    org, bucket = setting('INFLUXDB_ORG', 'atropos'), setting('INFLUXDB_BUCKET', 'metrics-replay')
    token = setting('INFLUXDB_TOKEN', 'atropos-local-replay-token')
    auth = 'Basic '+base64.b64encode((setting('GRAFANA_ADMIN_USER', 'admin')+':'+
                                    setting('GRAFANA_ADMIN_PASSWORD', 'admin')).encode()).decode()
    grafana_headers = {'Authorization': auth, 'X-Grafana-Org-Id': '1'}
    def request(url, data=None, headers=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Snapshot reader requires local HTTP endpoints')
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
        try:
            connection.request('POST' if data is not None else 'GET',
                               parsed.path+('?' + parsed.query if parsed.query else ''),
                               body=data, headers=headers or {})
            response = connection.getresponse()
            text = response.read().decode('utf-8')
            if response.status != 200:
                raise RuntimeError(f'Local read returned HTTP {response.status} at {parsed.path}')
            return text
        finally:
            connection.close()
    dashboard = json.loads(request(grafana+'/api/dashboards/uid/'+UID, headers=grafana_headers))['dashboard']
    panel = next(p for p in dashboard['panels'] if p.get('id') == 1)
    if panel.get('fieldConfig', {}).get('defaults', {}).get('unit') != 'ms':
        raise ValueError('Inspect changed panel units before loading service values')
    fixtures = Path(fixtures)
    fixtures.mkdir(parents=True, exist_ok=True)
    observed, queries, payload = {}, {}, []
    for target in panel['targets']:
        template = target.get('query', '')
        match = re.search(r'r\.service\s*==\s*"(service[1-4])"', template)
        if not match or '"service_response_time"' not in template or 'aggregateWindow' in template:
            raise ValueError('Inspect changed service query before reading the snapshot')
        service = match[1]
        if service in observed:
            raise ValueError('Expected exactly one query per service')
        query = (template.replace('v.defaultBucket', json.dumps(bucket))
                 .replace('v.timeRangeStart', START).replace('v.timeRangeStop', STOP))
        raw = request(influx+'/api/v2/query?'+urllib.parse.urlencode({'org': org}), query.encode(),
                      {'Authorization': 'Token '+token, 'Content-Type': 'application/vnd.flux', 'Accept': 'application/csv'})
        observed[service] = parse_points(raw)
        queries[service] = query
        (fixtures/(STEM+'-'+service+'-raw.csv')).write_text(raw, encoding='utf-8')
        (fixtures/(STEM+'-'+service+'.flux')).write_text(query, encoding='utf-8')
        payload.append({'refId': target['refId'], 'datasource': target.get('datasource', panel['datasource']),
                        'query': query, 'intervalMs': 15000, 'maxDataPoints': 10000})
    if set(observed) != {f'service{i}' for i in range(1, 5)}:
        raise ValueError('Expected exactly four services')
    body = {'from': str(int(datetime.fromisoformat(START.replace('Z', '+00:00')).timestamp()*1000)),
            'to': str(int(datetime.fromisoformat(STOP.replace('Z', '+00:00')).timestamp()*1000)), 'queries': payload}
    frames = json.loads(request(grafana+'/api/ds/query', json.dumps(body).encode(),
                                {**grafana_headers, 'Content-Type': 'application/json'}))['results']
    for target in panel['targets']:
        service = re.search(r'r\.service\s*==\s*"(service[1-4])"', target['query'])[1]
        answer = frames[target['refId']]
        if answer.get('error'):
            raise ValueError('Grafana service query returned an error')
        actual = []
        for frame in answer['frames']:
            fields, data = frame['schema']['fields'], frame['data']['values']
            time_index = next(i for i, field in enumerate(fields) if field['type'] == 'time')
            for i, field in enumerate(fields):
                if field['type'] == 'number':
                    actual.extend((int(t), float(v)) for t, v in zip(data[time_index], data[i]))
        expected = [(int(t.timestamp()*1000), value) for t, value in observed[service]]
        if sorted(actual) != expected:
            raise ValueError(f'Grafana and InfluxDB differ for {service}')
    path = fixtures/(STEM+'.csv')
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(('service', 'timestamp_utc', 'value_ms'))
        for service, points in sorted(observed.items()):
            writer.writerows((service, t.isoformat().replace('+00:00', 'Z'), repr(v)) for t, v in points)
    (fixtures/(STEM+'-dashboard.json')).write_text(json.dumps(dashboard, ensure_ascii=False, indent=2), encoding='utf-8')
    metadata = {'snapshot_date': datetime.now(timezone.utc).date().isoformat(),
                'dashboard_uid': UID, 'dashboard_title': dashboard['title'], 'panel_id': 1,
                'measurement': 'service_response_time', 'field': 'value', 'units': 'ms',
                'bucket': bucket, 'org': org, 'start_utc': START, 'stop_utc_inclusive': STOP,
                'interval_seconds': 15, 'total_points': sum(len(p) for p in observed.values()),
                'grafana_frames_equal_influx': True, 'read_only': True,
                'source_kind': 'Live InfluxDB snapshot of the previously image-reconstructed service response replay',
                'csv_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'services': {service: {'points': len(points), 'actual_start_utc': points[0][0].isoformat(),
                                       'actual_end_utc': points[-1][0].isoformat(),
                                       'min_ms': min(v for _, v in points), 'max_ms': max(v for _, v in points),
                                       'values_sha256': hashlib.sha256(np.asarray([v for _, v in points], dtype='<f8').tobytes()).hexdigest()}
                             for service, points in sorted(observed.items())}}
    (fixtures/(STEM+'.json')).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    return metadata


def load_snapshots(fixtures=FIXTURES):
    fixtures = Path(fixtures)
    metadata = json.loads((fixtures/(STEM+'.json')).read_text(encoding='utf-8'))
    path = fixtures/(STEM+'.csv')
    if hashlib.sha256(path.read_bytes()).hexdigest() != metadata['csv_sha256']:
        raise ValueError('Service snapshot checksum mismatch')
    with path.open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    cases = []
    for service, info in metadata['services'].items():
        points = [r for r in rows if r['service'] == service]
        values = np.asarray([float(r['value_ms']) for r in points])
        times = tuple(datetime.fromisoformat(r['timestamp_utc'].replace('Z', '+00:00')) for r in points)
        if len(points) != info['points'] or not np.isfinite(values).all():
            raise ValueError(f'Invalid service snapshot: {service}')
        if any((b-a).total_seconds() != 15 for a, b in zip(times, times[1:])):
            raise ValueError('Service snapshot requires the original 15-second grid')
        if hashlib.sha256(values.astype('<f8').tobytes()).hexdigest() != info['values_sha256']:
            raise ValueError(f'Service value fingerprint mismatch: {service}')
        cases.append(ReplayCase(service+'-20261005', 'Время отклика '+service+', 05.10.2026',
                                values, times, metadata['source_kind']))
    if sum(len(c.values) for c in cases) != metadata['total_points']:
        raise ValueError('Service snapshot point count mismatch')
    return cases, metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--infra-root', type=Path)
    args = parser.parse_args()
    if not args.fetch:
        parser.error('Use --fetch to explicitly refresh the snapshot')
    print(json.dumps(fetch_snapshot(infra_root=args.infra_root), ensure_ascii=False, indent=2))
