"""Import this replay and verify raw values through both InfluxDB and Grafana APIs."""
import base64
import csv
from datetime import datetime
import io
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
os.environ.pop('SSLKEYLOGFILE', None)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
INFLUX = os.environ.get('INFLUX_HOST', 'http://localhost:8086')
ORG = os.environ.get('INFLUXDB_ORG', 'atropos')
BUCKET = os.environ.get('INFLUXDB_BUCKET', 'metrics-replay')
TOKEN = os.environ.get('INFLUXDB_TOKEN', 'atropos-local-replay-token')
GRAFANA = os.environ.get('GRAFANA_URL', 'http://localhost:3001')
USER = os.environ.get('GRAFANA_ADMIN_USER', 'admin')
PASSWORD = os.environ.get('GRAFANA_ADMIN_PASSWORD', 'admin')
AUTH = {'Authorization': 'Basic ' + base64.b64encode(f'{USER}:{PASSWORD}'.encode()).decode()}


def request(url, data=None, headers=None):
    try:
        with OPENER.open(urllib.request.Request(url, data=data, headers=headers or {}), timeout=45) as response:
            return response.read().decode()
    except urllib.error.HTTPError as error:
        if error.code != 404:
            print(f'HTTP {error.code}: {error.read().decode()[:700]}')
        raise


def main():
    meta = json.loads((ROOT / 'metadata.json').read_text(encoding='utf-8'))
    dashboard = json.loads((ROOT / 'cusum-rps-20260922.json').read_text(encoding='utf-8'))
    assert ORG == meta['organization'] and BUCKET == meta['bucket'], 'Regenerate dashboard for a different bucket/org'
    assert json.loads(request(INFLUX + '/health'))['status'] == 'pass'
    health = json.loads(request(GRAFANA + '/api/datasources/uid/influxdb-replay/health', headers=AUTH))
    assert health['status'] == 'OK', health
    with (ROOT / 'rps.csv').open(encoding='utf-8', newline='') as source:
        expected = [(int(datetime.fromisoformat(row['timestamp']).timestamp()) * 1000, float(row['rps'])) for row in csv.DictReader(source)]
    assert len(expected) == meta['points']
    assert all(b[0] - a[0] == 15000 for a, b in zip(expected, expected[1:]))
    params = urllib.parse.urlencode({'org': ORG, 'bucket': BUCKET, 'precision': 's'})
    influx_headers = {'Authorization': 'Token ' + TOKEN}
    request(INFLUX + '/api/v2/write?' + params, (ROOT / 'rps.lp').read_bytes(), {**influx_headers, 'Content-Type': 'text/plain; charset=utf-8'})
    query = dashboard['panels'][0]['targets'][0]['query']
    utc_start = dashboard['time']['from']
    utc_end = dashboard['time']['to']
    flux = query.replace('v.defaultBucket', json.dumps(BUCKET)).replace('v.timeRangeStart', utc_start).replace('v.timeRangeStop', utc_end)
    result = request(INFLUX + '/api/v2/query?' + urllib.parse.urlencode({'org': ORG}), flux.encode(),
                     {**influx_headers, 'Content-Type': 'application/vnd.flux', 'Accept': 'application/csv'})
    lines = [line for line in result.splitlines() if line and not line.startswith('#')]
    actual = [(int(datetime.fromisoformat(row['_time'].replace('Z', '+00:00')).timestamp()) * 1000, float(row['_value'])) for row in csv.DictReader(lines)]
    assert actual == expected, (len(actual), len(expected))
    # Do not overwrite an unrelated dashboard if this UID already exists.
    try:
        existing = json.loads(request(GRAFANA + '/api/dashboards/uid/' + dashboard['uid'], headers=AUTH))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        existing = None
    if existing:
        assert existing['dashboard']['title'] == dashboard['title']
        assert existing['dashboard']['panels'][0]['targets'][0]['query'] == query
        saved = {'uid': dashboard['uid'], 'existing': True}
    else:
        body = {'dashboard': dashboard, 'overwrite': False, 'message': 'Add reconstructed 15-second RPS replay for CUSUM report'}
        saved = json.loads(request(GRAFANA + '/api/dashboards/db', json.dumps(body).encode(), {**AUTH, 'Content-Type': 'application/json'}))
    ds_body = {'from': str(expected[0][0]), 'to': str(expected[-1][0]),
               'queries': [{'refId': 'A', 'datasource': {'type': 'influxdb', 'uid': 'influxdb-replay'},
                            'query': query, 'intervalMs': 15000, 'maxDataPoints': 10000}]}
    ds_result = json.loads(request(GRAFANA + '/api/ds/query', json.dumps(ds_body).encode(), {**AUTH, 'Content-Type': 'application/json'}))
    answer = ds_result['results']['A']
    assert not answer.get('error'), answer.get('error')
    frames = answer['frames']
    assert len(frames) == 1, len(frames)
    frame = frames[0]
    fields = frame['schema']['fields']
    t = next(i for i, f in enumerate(fields) if f['type'] == 'time')
    v = next(i for i, f in enumerate(fields) if f['type'] == 'number')
    grafana = list(zip(frame['data']['values'][t], frame['data']['values'][v]))
    assert grafana == expected, (len(grafana), len(expected))
    verification = {'influxdb_exact_match_points': len(actual), 'grafana_exact_match_points': len(grafana),
                    'sample_step_seconds': 15, 'datasource_status': health['status'],
                    'dashboard_uid': saved['uid'], 'start': meta['start'], 'end': meta['end'],
                    'min_rps': min(v for _, v in actual), 'max_rps': max(v for _, v in actual)}
    (ROOT / 'verification.json').write_text(json.dumps(verification, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(verification, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
