"""Read-only snapshot of the corrected four-trace Scenario, shared by ACF/CUSUM.

Tests only read the frozen CSV and its checksums. Refresh explicitly with
python -m tests.scenario_influx --fetch; no database or dashboard writes occur.
"""
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
import shutil
from urllib.parse import urlencode, urlsplit

import numpy as np

FIXTURES = Path(__file__).with_name('fixtures')
STEM = 'scenario-influx-20261017'
UID = 'screenshot-steps-20261017'
MEASUREMENT = 'screenshot_step_replay'
TRACES = ('orange', 'green', 'yellow', 'blue')
COLORS = dict(zip(TRACES, ('#FF9830', '#73BF69', '#F2CC0C', '#8AB8FF')))
LABELS = dict(zip(TRACES, ('Оранжевый', 'Зелёный', 'Жёлтый', 'Голубой')))
START = '2026-10-17T11:23:30Z'
STOP = '2026-10-18T03:52:16Z'


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
        if not row.get('_time'):
            continue
        stamp = datetime.fromisoformat(row['_time'].replace('Z', '+00:00')).astimezone(timezone.utc)
        value = float(row['_value'])
        trace = row['trace']
        if trace not in TRACES or not math.isfinite(value):
            raise ValueError('Unexpected Scenario trace or nonfinite value')
        points.append((trace, stamp, value))
    validate_points(points)
    return sorted(points)


def validate_points(points):
    if len(points) != 15824 or {p[0] for p in points} != set(TRACES):
        raise ValueError('Expected four complete Scenario traces, 15824 points')
    for trace in TRACES:
        series = sorted((t, v) for name, t, v in points if name == trace)
        if len(series) != 3956 or not all(math.isfinite(v) for _, v in series):
            raise ValueError('Scenario series has missing/nonfinite points')
        if any((b[0]-a[0]).total_seconds() != 15 for a, b in zip(series, series[1:])):
            raise ValueError('Scenario requires unique timestamps on the original 15-second grid')
        if series[0][0].isoformat() != '2026-10-17T11:23:30+00:00' or series[-1][0].isoformat() != '2026-10-18T03:52:15+00:00':
            raise ValueError('Scenario dates differ from the requested October 17--18 history')


def request(url, data=None, headers=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Snapshot reader accepts local HTTP endpoints only')
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=45)
    try:
        conn.request('POST' if data is not None else 'GET', parsed.path+('?' + parsed.query if parsed.query else ''), body=data, headers=headers or {})
        response = conn.getresponse()
        text = response.read().decode('utf-8')
        if response.status != 200:
            raise RuntimeError(f'Snapshot read failed: HTTP {response.status} at {parsed.path}')
        return text
    finally:
        conn.close()


def fetch_snapshot(fixtures=FIXTURES, reference_dir=None):
    influx = os.getenv('INFLUX_URL', 'http://127.0.0.1:8086').rstrip('/')
    grafana = os.getenv('GRAFANA_URL', 'http://127.0.0.1:3001').rstrip('/')
    org, bucket = os.getenv('INFLUXDB_ORG', 'atropos'), os.getenv('INFLUXDB_BUCKET', 'metrics-replay')
    auth = {'Authorization': 'Basic '+base64.b64encode((os.getenv('GRAFANA_ADMIN_USER', 'admin')+':'+os.getenv('GRAFANA_ADMIN_PASSWORD', 'admin')).encode()).decode()}
    dashboard = json.loads(request(grafana+'/api/dashboards/uid/'+UID, headers=auth))['dashboard']
    panel = next(p for p in dashboard['panels'] if p['id'] == 1)
    if panel['title'] != 'Сценарий' or len(panel['targets']) != 4:
        raise ValueError('Inspect changed Scenario dashboard before refreshing')
    query = (f'from(bucket: {json.dumps(bucket)}) |> range(start: {START}, stop: {STOP})\n'
        f' |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "value")\n'
        ' |> keep(columns: ["_time", "_value", "trace"]) |> group(columns: ["trace"]) |> sort(columns: ["_time"])')
    raw = request(influx+'/api/v2/query?'+urlencode({'org': org}), query.encode(),
        {'Authorization': 'Token '+os.getenv('INFLUXDB_TOKEN', 'atropos-local-replay-token'),
            'Content-Type': 'application/vnd.flux', 'Accept': 'application/csv'})
    points = parse_points(raw)
    expected = {(trace, int(t.timestamp()*1000)): v for trace, t, v in points}
    targets = []
    refs = {}
    for target in panel['targets']:
        trace = next(s for s in TRACES if f'r.trace == "{s}"' in target['query'])
        if 'aggregateWindow' in target['query']:
            raise ValueError('Scenario query must retain every raw point')
        refs[target['refId']] = trace
        targets.append({**target, 'intervalMs': 15000, 'maxDataPoints': 20000})
    body = {'from': str(int(datetime.fromisoformat(START.replace('Z','+00:00')).timestamp()*1000)),
        'to': str(int(datetime.fromisoformat(STOP.replace('Z','+00:00')).timestamp()*1000)), 'queries': targets}
    results = json.loads(request(grafana+'/api/ds/query', json.dumps(body).encode(), {**auth, 'Content-Type': 'application/json'}))['results']
    rendered = {}
    for ref, trace in refs.items():
        if results[ref].get('error'):
            raise ValueError('Grafana Scenario query failed')
        for frame in results[ref].get('frames', []):
            fields, data = frame['schema']['fields'], frame['data']['values']
            ti = next(i for i, field in enumerate(fields) if field['type'] == 'time')
            vi = next(i for i, field in enumerate(fields) if field['type'] == 'number')
            rendered.update(((trace, int(t)), float(v)) for t, v in zip(data[ti], data[vi]))
    if rendered != expected:
        raise ValueError('Grafana does not match the direct InfluxDB snapshot')
    fixtures = Path(fixtures)
    fixtures.mkdir(exist_ok=True, parents=True)
    path = fixtures/(STEM+'.csv')
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(('timestamp_utc', 'trace', 'value_tps'))
        writer.writerows((t.isoformat().replace('+00:00','Z'), s, repr(v)) for s, t, v in points)
    (fixtures/(STEM+'-raw.csv')).write_text(raw, encoding='utf-8')
    (fixtures/(STEM+'-query.flux')).write_text(query+'\n', encoding='utf-8')
    (fixtures/(STEM+'-dashboard.json')).write_text(json.dumps(dashboard, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    hashes = {s: hashlib.sha256(np.array([v for name, _, v in points if name == s], dtype='<f8').tobytes()).hexdigest() for s in TRACES}
    metadata = {'fetched_at_utc': datetime.now(timezone.utc).isoformat(), 'measurement': MEASUREMENT,
        'field': 'value', 'tag': 'trace', 'traces': list(TRACES), 'unit': 'TPS', 'org': org, 'bucket': bucket,
        'dashboard_uid': UID, 'dashboard_url': grafana+'/d/'+UID, 'read_only': True,
        'grafana_frames_equal_influx': True, 'points': len(points), 'points_per_trace': 3956,
        'sample_interval_seconds': 15, 'start_utc': START, 'end_utc': '2026-10-18T03:52:15Z',
        'csv_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'values_sha256': hashes,
        'source_kind': 'Frozen live InfluxDB snapshot of corrected image-reconstructed Scenario; TPS',
        'is_raw_production_telemetry': False, 'reference_files': []}
    if reference_dir is not None:
        reference_dir = Path(reference_dir)
        for source, name in [(reference_dir/'source'/'screenshot-step-replay-reference.png', STEM+'-reference.png'),
                (reference_dir/'source'/'orange-high-plateau-reference.png', STEM+'-detail-reference.png'),
                (reference_dir/'upper-plateau-comparison.png', STEM+'-comparison.png')]:
            shutil.copy2(source, fixtures/name)
            metadata['reference_files'].append(name)
    (fixtures/(STEM+'.json')).write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return metadata


def load_snapshot(fixtures=FIXTURES):
    fixtures = Path(fixtures)
    metadata = json.loads((fixtures/(STEM+'.json')).read_text(encoding='utf-8'))
    path = fixtures/(STEM+'.csv')
    if hashlib.sha256(path.read_bytes()).hexdigest() != metadata['csv_sha256']:
        raise ValueError('Scenario snapshot CSV checksum mismatch')
    with path.open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    points = [(r['trace'], datetime.fromisoformat(r['timestamp_utc'].replace('Z','+00:00')), float(r['value_tps'])) for r in rows]
    validate_points(points)
    groups = {}
    for trace in TRACES:
        pairs = sorted((t, v) for s, t, v in points if s == trace)
        values = np.asarray([v for _, v in pairs], dtype=float)
        if hashlib.sha256(values.astype('<f8').tobytes()).hexdigest() != metadata['values_sha256'][trace]:
            raise ValueError('Scenario values checksum mismatch')
        groups[trace] = (tuple(t for t, _ in pairs), values)
    return groups, metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true', required=True)
    parser.add_argument('--reference-dir', type=Path, default=Path(__file__).resolve().parents[2]/'infra/influxdb/step-screenshot')
    args = parser.parse_args()
    meta = fetch_snapshot(reference_dir=args.reference_dir)
    print(json.dumps({k: meta[k] for k in ['points', 'unit', 'grafana_frames_equal_influx', 'values_sha256']}, indent=2))
