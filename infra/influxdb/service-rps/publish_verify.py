"""Import the new measurement and verify InfluxDB and Grafana against the CSV."""
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
from urllib.request import Request, urlopen
from urllib.error import HTTPError

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
        detail = error.read().decode(errors='replace')
        raise RuntimeError(f'HTTP {error.code}: {detail[:1024]}') from error


def query(flux):
    raw = request(INFLUX+'/api/v2/query?'+urlencode({'org': ORG}), flux.encode(),
                  {'Authorization': 'Token '+TOKEN, 'Content-Type': 'application/vnd.flux', 'Accept': 'application/csv'})
    header, rows = None, []
    for fields in csv.reader(io.StringIO(raw)):
        if not fields or fields[0].startswith('#'):
            continue
        if '_value' in fields:
            header = fields
            continue
        if header:
            rows.append(dict(zip(header, fields)))
    return rows


def history_counts():
    flux = (f'from(bucket: {json.dumps(BUCKET)}) |> range(start: 0, stop: 2100-01-01T00:00:00Z)'
            ' |> group(columns: ["_measurement", "_field"]) |> count(column: "_value")')
    return {r['_measurement']+'/'+r['_field']: int(r['_value']) for r in query(flux)}


def local_checks():
    meta = json.loads((ROOT/'metadata.json').read_text(encoding='utf-8'))
    points = [(datetime.fromisoformat(r['timestamp']), r['service'], float(r['rps']))
              for r in csv.DictReader((ROOT/'metrics.csv').open(encoding='utf-8'))]
    expected = {(s, int(t.timestamp())): v for t, s, v in points}
    checks = {
        'unique_points': len(expected) == len(points) == meta['points'],
        'five_requested_service_names': {s for _, s, _ in points} == {f'service{i}' for i in range(1, 6)},
        'all_dates_october_4_moscow': all(t.date().isoformat() == '2026-10-04' and t.utcoffset().total_seconds() == 10800 for t, _, _ in points),
        'finite_values_within_image_scale': all(math.isfinite(v) and 1000 < v < 2000 for _, _, v in points),
        'line_protocol_uses_lf_and_has_all_points': b'\r' not in (ROOT/'metrics.lp').read_bytes() and len((ROOT/'metrics.lp').read_bytes().splitlines()) == len(points),
        'all_series_have_uniform_15_second_step': all(all(b-a == 15 for a, b in zip(ts, ts[1:]))
             for s in meta['services'] for ts in [sorted(t for name, t in expected if name == s)]),
        'green_has_no_invented_early_history': min(t for name, t in expected if name == 'service1') == int(datetime.fromisoformat('2026-10-04T22:50:15+03:00').timestamp()),
    }
    baseline, late = {}, {}
    for service in meta['services']:
        baseline[service] = [v for t, s, v in points if s == service and '21:00' <= t.strftime('%H:%M') < '22:30']
        late[service] = [v for t, s, v in points if s == service and t.strftime('%H:%M') >= '23:00']
    checks['sharp_drop_preserved_for_four_services'] = all(statistics.fmean(late[s]) < .9*statistics.fmean(baseline[s]) for s in ['service2', 'service3', 'service4', 'service5'])
    checks['green_late_level_matches_screenshot'] = 1880 < statistics.fmean(late['service1']) < 1930
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
        assert not any(k.startswith('service_rps/') for k in counts), 'Measurement already exists; inspect before changing it'
        (ROOT/'before.json').write_text(json.dumps(counts, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({'existing_measurement_fields': len(counts), 'existing_field_values': sum(counts.values())})); return
    before = json.loads((ROOT/'before.json').read_text(encoding='utf-8'))
    if phase == 'write':
        current = history_counts()
        assert current == before, 'History changed after preflight; inspect before importing'
        request(INFLUX+'/api/v2/write?'+urlencode({'org': ORG, 'bucket': BUCKET, 'precision': 's'}),
                (ROOT/'metrics.lp').read_bytes(), {'Authorization': 'Token '+TOKEN, 'Content-Type': 'text/plain; charset=utf-8'})
        print(f'Imported {len(expected)} service RPS points'); return
    rows = query(f'from(bucket: {json.dumps(BUCKET)}) |> range(start: 0, stop: 2100-01-01T00:00:00Z)'
                 ' |> filter(fn: (r) => r._measurement == "service_rps")')
    actual = {(r['service'], int(datetime.fromisoformat(r['_time'].replace('Z', '+00:00')).timestamp())): float(r['_value']) for r in rows}
    checks['database_all_points_and_tags_match_exactly'] = actual == expected and len(rows) == len(expected) and {r['_field'] for r in rows} == {'value'}
    after = history_counts()
    checks['existing_history_counts_unchanged'] = {k: v for k, v in after.items() if not k.startswith('service_rps/')} == before
    response = json.loads(request(GRAFANA+'/api/dashboards/uid/'+meta['dashboard_uid'], headers=AUTH))
    dashboard = response['dashboard']; panel = dashboard['panels'][0]
    checks['dashboard_dates_and_timezone_correct'] = dashboard['timezone'] == 'Europe/Moscow' and dashboard['time'] == {'from': '2026-10-04T17:27:00Z', 'to': '2026-10-04T20:27:00Z'}
    checks['dashboard_rps_unit_and_raw_samples'] = panel['fieldConfig']['defaults']['unit'] == 'reqps' and all('aggregateWindow' not in t['query'] for t in panel['targets'])
    service_overrides = [o for o in panel['fieldConfig']['overrides'] if o['matcher']['options'] in 'ABCDE']
    checks['dashboard_service_names_and_colors'] = [o['properties'][0]['value'] for o in service_overrides] == list(meta['services'])
    payload = {'queries': [{**t, 'intervalMs': 15000, 'maxDataPoints': 10000} for t in panel['targets']]}
    # Derive query bounds from the dashboard instead of relying on host timezone.
    payload.update({key: str(int(datetime.fromisoformat(dashboard['time'][key].replace('Z', '+00:00')).timestamp()*1000)) for key in ['from', 'to']})
    results = json.loads(request(GRAFANA+'/api/ds/query', json.dumps(payload).encode(), {**AUTH, 'Content-Type': 'application/json'}))['results']
    rendered = {}; frames = 0
    for ref, service in zip('ABCDE', meta['services']):
        result = results[ref]
        assert not result.get('error'), result.get('error')
        for frame in result.get('frames', []):
            frames += 1
            fields = frame['schema']['fields']; data = frame['data']['values']
            ti = next(i for i, f in enumerate(fields) if f['type'] == 'time')
            vi = next(i for i, f in enumerate(fields) if f['type'] == 'number')
            rendered.update(((service, int(t/1000)), v) for t, v in zip(data[ti], data[vi]))
    checks['grafana_returns_all_five_raw_series_exactly'] = rendered == expected and frames == 5
    expected_sum = {}
    for (_, stamp), value in expected.items():
        expected_sum[stamp] = expected_sum.get(stamp, 0.0)+value
    total_result = results['F']
    assert not total_result.get('error'), total_result.get('error')
    total = {}
    for frame in total_result.get('frames', []):
        fields = frame['schema']['fields']; data = frame['data']['values']
        ti = next(i for i, field in enumerate(fields) if field['type'] == 'time')
        vi = next(i for i, field in enumerate(fields) if field['type'] == 'number')
        total.update((int(t/1000), value) for t, value in zip(data[ti], data[vi]))
    checks['sum_matches_available_services_at_every_timestamp'] = total.keys() == expected_sum.keys() and all(math.isclose(total[t], v, rel_tol=0, abs_tol=1e-8) for t, v in expected_sum.items())
    checks['sum_has_one_time_series'] = len(total_result.get('frames', [])) == 1
    total_override = next(o for o in panel['fieldConfig']['overrides'] if o['matcher']['options'] == 'F')
    total_properties = {p['id']: p['value'] for p in total_override['properties']}
    checks['sum_uses_separate_right_axis_and_visible_range'] = total_properties.get('custom.axisPlacement') == 'right' and total_properties.get('max', 0) > max(total.values())
    result = {'passed': all(checks.values()), 'checks': checks, 'points': len(expected), 'before': before, 'after': after,
              'dashboard_url': GRAFANA+response['meta']['url'], 'metadata': meta}
    (ROOT/'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ['passed', 'checks', 'points', 'dashboard_url']}, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
