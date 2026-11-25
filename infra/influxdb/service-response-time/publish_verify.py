"""Import and verify the independent October 5 service latency replay."""
import argparse
import base64
import csv
import io
import json
import math
import os
import statistics
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

os.environ.pop('SSLKEYLOGFILE', None)
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
INFLUX = os.getenv('INFLUX_URL', 'http://127.0.0.1:8086').rstrip('/')
GRAFANA = os.getenv('GRAFANA_URL', 'http://127.0.0.1:3001').rstrip('/')
ORG = os.getenv('INFLUXDB_ORG', 'atropos')
BUCKET = os.getenv('INFLUXDB_BUCKET', 'metrics-replay')
TOKEN = os.getenv('INFLUXDB_TOKEN', 'atropos-local-replay-token')
AUTH = {'Authorization': 'Basic '+base64.b64encode((os.getenv('GRAFANA_ADMIN_USER', 'admin')+':'+os.getenv('GRAFANA_ADMIN_PASSWORD', 'admin')).encode()).decode()}


def request(url, data=None, headers=None):
    try:
        with urlopen(Request(url, data=data, headers=headers or {}), timeout=60) as response:
            return response.read().decode()
    except HTTPError as error:
        raise RuntimeError(f'HTTP {error.code}: {error.read().decode(errors="replace")[:1024]}') from error


def query(flux):
    raw = request(INFLUX+'/api/v2/query?'+urlencode({'org': ORG}), flux.encode(),
                  {'Authorization': 'Token '+TOKEN, 'Content-Type': 'application/vnd.flux', 'Accept': 'application/csv'})
    header, rows = None, []
    for fields in csv.reader(io.StringIO(raw)):
        if not fields or fields[0].startswith('#'): continue
        if '_value' in fields: header = fields; continue
        if header: rows.append(dict(zip(header, fields)))
    return rows


def history_counts():
    rows = query(f'from(bucket: {json.dumps(BUCKET)}) |> range(start: 0, stop: 2100-01-01T00:00:00Z)'
                 ' |> group(columns: ["_measurement", "_field"]) |> count(column: "_value")')
    return {r['_measurement']+'/'+r['_field']: int(r['_value']) for r in rows}


def local_checks():
    meta = json.loads((ROOT/'metadata.json').read_text(encoding='utf-8'))
    points = [(datetime.fromisoformat(r['timestamp']), r['service'], float(r['response_time_ms']))
              for r in csv.DictReader((ROOT/'metrics.csv').open(encoding='utf-8'))]
    expected = {(s, int(t.timestamp())): v for t, s, v in points}
    lp = (ROOT/'metrics.lp').read_bytes()
    per_service = {s: [(t, v) for t, name, v in points if name == s] for s in meta['services']}
    baseline = {s: [v for t, v in rows if t.hour < 4] for s, rows in per_service.items()}
    late = {s: [v for t, v in rows if t.hour >= 10] for s, rows in per_service.items()}
    checks = {
        'four_series_and_unique_points': len(expected) == len(points) == meta['points'] == 11364 and set(per_service) == {f'service{i}' for i in range(1, 5)},
        'unit_ms_and_exclusion_of_blue_border_confirmed': meta['unit'] == 'ms' and meta['unit_confirmed_by_user'] and meta['four_series_excluding_blue_border_confirmed_by_user'],
        'all_local_dates_october_5_moscow': all(t.date().isoformat() == '2026-10-05' and t.utcoffset().total_seconds() == 10800 for t, _, _ in points),
        'all_services_have_2841_points_at_15_seconds': all(len(rows) == 2841 and all((b[0]-a[0]).total_seconds() == 15 for a, b in zip(rows, rows[1:])) for rows in per_service.values()),
        'all_services_cover_0000_to_1150': all(rows[0][0].isoformat() == meta['start'] and rows[-1][0].isoformat() == meta['end'] for rows in per_service.values()),
        'finite_nonnegative_ms': all(math.isfinite(v) and 0 < v < 100 for _, _, v in points),
        'approximate_requested_baselines': all(abs(statistics.fmean(baseline[s])-target) < 3 for s, target in [('service1', 40), ('service2', 70), ('service3', 53), ('service4', 40)]),
        'increased_late_variance_preserved': all(statistics.pstdev(late[s]) > 3*statistics.pstdev(baseline[s]) for s in per_service),
        'large_visible_excursions_retained': min(v for _, v in per_service['service1']) < 20 and max(v for _, v in per_service['service2']) > 85,
        'line_protocol_lf_and_point_count': b'\r' not in lp and len(lp.splitlines()) == len(points),
    }
    assert all(checks.values()), checks
    return expected, meta, checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['draft', 'before', 'write', 'after'])
    phase = parser.parse_args().phase
    expected, meta, checks = local_checks()
    if phase == 'draft':
        result = {'passed': True, 'points': len(expected), 'checks': checks}
        (ROOT/'draft-verification.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(result, indent=2)); return
    if phase == 'before':
        counts = history_counts()
        assert not any(k.startswith('service_response_time/') for k in counts), 'New measurement conflicts with existing history'
        dashboards = json.loads(request(GRAFANA+'/api/search?type=dash-db', headers=AUTH))
        assert all(d['uid'] != meta['dashboard_uid'] for d in dashboards), 'Dashboard UID already exists'
        (ROOT/'before.json').write_text(json.dumps(counts, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({'existing_measurement_fields': len(counts), 'existing_field_values': sum(counts.values())})); return
    before = json.loads((ROOT/'before.json').read_text(encoding='utf-8'))
    if phase == 'write':
        assert history_counts() == before, 'History changed since preflight; inspect before importing'
        request(INFLUX+'/api/v2/write?'+urlencode({'org': ORG, 'bucket': BUCKET, 'precision': 's'}),
                (ROOT/'metrics.lp').read_bytes(), {'Authorization': 'Token '+TOKEN, 'Content-Type': 'text/plain; charset=utf-8'})
        print(f'Imported {len(expected)} response-time points'); return
    rows = query(f'from(bucket: {json.dumps(BUCKET)}) |> range(start: 0, stop: 2100-01-01T00:00:00Z)'
                 ' |> filter(fn: (r) => r._measurement == "service_response_time")')
    actual = {(r['service'], int(datetime.fromisoformat(r['_time'].replace('Z', '+00:00')).timestamp())): float(r['_value']) for r in rows}
    checks['influxdb_all_values_dates_and_tags_match_exactly'] = actual == expected and len(rows) == len(expected) and {r['_field'] for r in rows} == {'value'}
    after = history_counts()
    checks['existing_history_counts_unchanged'] = {k: v for k, v in after.items() if not k.startswith('service_response_time/')} == before
    response = json.loads(request(GRAFANA+'/api/dashboards/uid/'+meta['dashboard_uid'], headers=AUTH))
    dashboard = response['dashboard']; panel = dashboard['panels'][0]
    checks['dashboard_uses_october_5_moscow'] = dashboard['timezone'] == 'Europe/Moscow' and dashboard['time'] == {'from': '2026-10-04T21:00:00Z', 'to': '2026-10-05T08:50:00Z'}
    checks['dashboard_uses_ms_and_raw_samples'] = panel['fieldConfig']['defaults']['unit'] == 'ms' and len(panel['targets']) == 4 and all('aggregateWindow' not in t['query'] for t in panel['targets'])
    checks['dashboard_four_service_names'] = [o['properties'][0]['value'] for o in panel['fieldConfig']['overrides']] == list(meta['services'])
    payload = {'queries': [{**t, 'intervalMs': 15000, 'maxDataPoints': 20000} for t in panel['targets']]}
    payload.update({key: str(int(datetime.fromisoformat(dashboard['time'][key].replace('Z', '+00:00')).timestamp()*1000)) for key in ['from', 'to']})
    results = json.loads(request(GRAFANA+'/api/ds/query', json.dumps(payload).encode(), {**AUTH, 'Content-Type': 'application/json'}))['results']
    rendered = {}; frames = 0
    for ref, service in zip('ABCD', meta['services']):
        result = results[ref]
        assert not result.get('error'), result.get('error')
        for frame in result.get('frames', []):
            frames += 1
            fields, data = frame['schema']['fields'], frame['data']['values']
            ti = next(i for i, f in enumerate(fields) if f['type'] == 'time')
            vi = next(i for i, f in enumerate(fields) if f['type'] == 'number')
            rendered.update(((service, int(t/1000)), v) for t, v in zip(data[ti], data[vi]))
    checks['grafana_returns_all_four_series_exactly'] = rendered == expected and frames == 4
    result = {'passed': all(checks.values()), 'points': len(expected), 'checks': checks, 'before': before, 'after': after,
              'dashboard_url': GRAFANA+response['meta']['url'], 'metadata': meta}
    (ROOT/'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ['passed', 'points', 'checks', 'dashboard_url']}, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__': main()
