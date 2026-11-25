"""Check artifacts, import the new replay, and verify exact live values."""
import argparse
import base64
import csv
import hashlib
import io
import json
import math
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

os.environ.pop('SSLKEYLOGFILE', None)
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DASHBOARD_ROOT = ROOT if (ROOT / 'dashboards').is_dir() else ROOT.parent
INFLUX = os.getenv('INFLUX_URL', 'http://127.0.0.1:8086').rstrip('/')
GRAFANA = os.getenv('GRAFANA_URL', 'http://127.0.0.1:3001').rstrip('/')
ORG = os.getenv('INFLUXDB_ORG', 'atropos')
BUCKET = os.getenv('INFLUXDB_BUCKET', 'metrics-replay')
TOKEN = os.getenv('INFLUXDB_TOKEN', 'atropos-local-replay-token')
MEASUREMENT = 'screenshot_step_replay'
UID = 'screenshot-steps-20261017'
AUTH = {'Authorization': 'Basic ' + base64.b64encode((os.getenv('GRAFANA_ADMIN_USER', 'admin') + ':' + os.getenv('GRAFANA_ADMIN_PASSWORD', 'admin')).encode()).decode()}


def request(url, data=None, headers=None):
    with urlopen(Request(url, data=data, headers=headers or {}), timeout=60) as response:
        return response.read().decode()


def query(flux):
    raw = request(INFLUX + '/api/v2/query?' + urlencode({'org': ORG}), flux.encode(),
        {'Authorization': 'Token ' + TOKEN, 'Content-Type': 'application/vnd.flux', 'Accept': 'application/csv'})
    header, rows = None, []
    for fields in csv.reader(io.StringIO(raw)):
        if not fields or fields[0].startswith('#'):
            continue
        if '_value' in fields:
            header = fields
        elif header:
            rows.append(dict(zip(header, fields)))
    return rows


def all_rows():
    return query(f'from(bucket: {json.dumps(BUCKET)}) |> range(start: 0, stop: 2100-01-01T00:00:00Z)')


def history_digest(rows):
    series = {}
    ignored = {'', 'result', 'table', '_start', '_stop', '_time', '_value'}
    for row in rows:
        if row['_measurement'] == MEASUREMENT:
            continue
        key = json.dumps(sorted((k, v) for k, v in row.items() if k not in ignored and v != ''), separators=(',', ':'))
        stamp = datetime.fromisoformat(row['_time'].replace('Z', '+00:00')).isoformat()
        series.setdefault(key, []).append((stamp, repr(float(row['_value']))))
    return {key: {'points': len(v), 'sha256': hashlib.sha256(json.dumps(sorted(v), separators=(',', ':')).encode()).hexdigest()}
        for key, v in sorted(series.items())}


def save(name, obj):
    (ROOT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def target_values(rows):
    return {(r['trace'], int(datetime.fromisoformat(r['_time'].replace('Z', '+00:00')).timestamp())): float(r['_value'])
        for r in rows if r['_measurement'] == MEASUREMENT}


def target_hash(values):
    return hashlib.sha256(json.dumps(sorted((s,t,repr(v)) for (s,t),v in values.items()), separators=(',', ':')).encode()).hexdigest()


def local():
    metadata = json.loads((ROOT / 'metadata.json').read_text(encoding='utf-8'))
    rows = list(csv.DictReader((ROOT / 'metrics.csv').open(encoding='utf-8')))
    expected = {(r['trace'], int(datetime.fromisoformat(r['timestamp']).timestamp())): float(r['value']) for r in rows}
    times = sorted({t for _, t in expected})
    checks = {
        'four_unique_series_and_all_points': len(expected) == len(rows) == 15824 and {s for s, _ in expected} == {'orange', 'green', 'yellow', 'blue'},
        'full_october_17_to_18_moscow_range': rows[0]['timestamp'] == '2026-10-17T14:23:30+03:00' and rows[-1]['timestamp'] == '2026-10-18T06:52:15+03:00',
        'all_four_series_uniform_15_seconds': all(sorted(t for name, t in expected if name == s) == times for s in metadata['traces']) and {b-a for a, b in zip(times, times[1:])} == {15},
        'both_source_image_hashes_match': all(hashlib.sha256((ROOT/'source'/name).read_bytes()).hexdigest() == digest for name,digest in metadata['sources'].items()),
        'finite_nonnegative_values': all(math.isfinite(v) and 0 <= v < 190 for v in expected.values()),
        'no_false_blue_edge_peak': max(v for (s, _), v in expected.items() if s == 'blue') < 105,
        'line_protocol_count_and_lf': len((ROOT / 'metrics.lp').read_bytes().splitlines()) == len(expected) and b'\r' not in (ROOT / 'metrics.lp').read_bytes(),
        'source_provenance_matches': metadata['csv_sha256'] == hashlib.sha256((ROOT / 'metrics.csv').read_bytes()).hexdigest(),
    }
    orange = [expected['orange',t] for t in times]
    widths = []
    for turn in metadata['turns']:
        if turn['sign'] != -1:
            continue
        i = turn['index']
        a = b = i
        while a > 0 and orange[a-1] < 164.8:
            a -= 1
        while b+1 < len(orange) and orange[b+1] < 164.8:
            b += 1
        widths.append(b-a+1)
    checks['seven_visible_rollbacks_below_upper_plateau'] = len(widths) == 7
    checks['rollback_widths_preserved_not_single_samples'] = min(widths) >= 5 and max(widths) >= 12
    checks['fifteen_upper_turns_follow_reference_pixels'] = len(metadata['turns']) == 15 and all(orange[t['index']] == t['value'] for t in metadata['turns'])
    checks['sloped_descent_and_recovery_around_troughs'] = all(
        orange[t['index']-2] > t['value'] and orange[t['index']+2] > t['value']
        for t in metadata['turns'] if t['sign'] < 0)
    checks['new_orange_data_has_not_reused_old_fixture'] = metadata['revision'] == 2 and metadata['detail_calibration']['narrow_excursions_collapsed'] is False
    metadata['rollback_width_samples'] = widths
    for trace, lower, upper in [('orange', 160, 185), ('green', 110, 130), ('yellow', 28, 38), ('blue', 14, 20)]:
        first = [float(r['value']) for r in rows if r['trace'] == trace and '2026-10-17T20:30' <= r['timestamp'] < '2026-10-17T22:00']
        quiet = [float(r['value']) for r in rows if r['trace'] == trace and '2026-10-18T02:00' <= r['timestamp'] < '2026-10-18T04:00']
        checks[trace + '_high_plateau_level'] = lower < sum(quiet)/len(quiet) < upper
        checks[trace + '_first_high_plateau_more_variable'] = max(first)-min(first) > max(quiet)-min(quiet)
    assert all(checks.values()), {k: v for k, v in checks.items() if not v}
    return expected, metadata, checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['draft', 'before', 'write', 'after'])
    phase = parser.parse_args().phase
    expected, meta, checks = local()
    if phase == 'draft':
        save('draft-verification.json', {'passed': True, 'checks': checks, 'points': len(expected)})
        print(json.dumps({'passed': True, 'checks': checks, 'points': len(expected)}, indent=2))
        return
    if phase == 'before':
        rows = all_rows()
        current = target_values(rows)
        backup = list(csv.DictReader((ROOT/'pre-install-backup'/'metrics.csv').open(encoding='utf-8')))
        old = {(r['trace'],int(datetime.fromisoformat(r['timestamp']).timestamp())):float(r['value']) for r in backup}
        assert current == old, 'Live replay changed since backup'
        assert current.keys() == expected.keys(), 'Correction must overwrite exactly the same timestamps and tags'
        snapshot = history_digest(rows)
        save('before.json', {'history':snapshot,'target_sha256':target_hash(current),'target_points':len(current)})
        print(json.dumps({'existing_series': len(snapshot), 'existing_values': sum(s['points'] for s in snapshot.values())}))
        return
    before = json.loads((ROOT / 'before.json').read_text(encoding='utf-8'))
    if phase == 'write':
        rows = all_rows()
        assert target_hash(target_values(rows)) == before['target_sha256'], 'Live replay changed after preflight'
        assert target_values(rows).keys() == expected.keys(), 'Unexpected target timestamps'
        assert history_digest(rows) == before['history'], 'Other history changed after preflight'
        request(INFLUX + '/api/v2/write?' + urlencode({'org': ORG, 'bucket': BUCKET, 'precision': 's'}),
            (ROOT / 'metrics.lp').read_bytes(), {'Authorization': 'Token ' + TOKEN, 'Content-Type': 'text/plain; charset=utf-8'})
        print(json.dumps({'imported_points': len(expected), 'measurement': MEASUREMENT}))
        return
    rows = all_rows()
    actual_rows = [r for r in rows if r['_measurement'] == MEASUREMENT]
    actual = {(r['trace'], int(datetime.fromisoformat(r['_time'].replace('Z', '+00:00')).timestamp())): float(r['_value']) for r in actual_rows}
    checks['influxdb_all_points_exactly_match'] = actual == expected and len(actual_rows) == len(expected) and {r['_field'] for r in actual_rows} == {'value'}
    checks['all_other_values_and_tags_unchanged'] = history_digest(rows) == before['history']
    live = json.loads(request(GRAFANA + '/api/dashboards/uid/' + UID, headers=AUTH))
    dashboard = live['dashboard']
    local_dash = json.loads((DASHBOARD_ROOT / 'dashboards' / (UID + '.json')).read_text(encoding='utf-8'))
    checks['grafana_dates_timezone_and_five_graphs'] = dashboard['time'] == local_dash['time'] and dashboard['timezone'] == 'Europe/Moscow' and sum(p['type'] == 'timeseries' for p in dashboard['panels']) == 5
    checks['no_query_aggregation'] = all('aggregateWindow' not in t['query'] for p in dashboard['panels'] for t in p.get('targets', []))
    checks['all_graphs_connect_points_linearly'] = all(p['fieldConfig']['defaults']['custom']['lineInterpolation'] == 'linear' for p in dashboard['panels'] if p['type'] == 'timeseries')
    orange_panel = next(p for p in dashboard['panels'] if p['id'] == 2)
    checks['orange_panel_y_axis_adapts_to_zoom'] = not ({'min','max'} & orange_panel['fieldConfig']['defaults'].keys())
    checks['scenario_title_and_tps_units'] = dashboard['panels'][0]['title'] == 'Сценарий' and all(p['fieldConfig']['defaults']['unit'] == 'suffix:TPS' and p['fieldConfig']['defaults']['custom']['axisLabel'] == 'TPS' for p in dashboard['panels'] if p['type'] == 'timeseries')
    panel = dashboard['panels'][0]
    payload = {'queries': [{**target, 'intervalMs': 15000, 'maxDataPoints': 20000} for target in panel['targets']]}
    for key in ['from', 'to']:
        payload[key] = str(int(datetime.fromisoformat(dashboard['time'][key].replace('Z', '+00:00')).timestamp() * 1000))
    results = json.loads(request(GRAFANA + '/api/ds/query', json.dumps(payload).encode(), {**AUTH, 'Content-Type': 'application/json'}))['results']
    rendered = {}
    frames = 0
    for ref, trace in zip('ABCD', reversed(meta['traces'])):
        result = results[ref]
        assert not result.get('error'), result.get('error')
        for frame in result.get('frames', []):
            frames += 1
            fields, data = frame['schema']['fields'], frame['data']['values']
            ti = next(i for i, f in enumerate(fields) if f['type'] == 'time')
            vi = next(i for i, f in enumerate(fields) if f['type'] == 'number')
            rendered.update(((trace, int(t/1000)), v) for t, v in zip(data[ti], data[vi]))
    checks['grafana_all_four_raw_series_match_exactly'] = rendered == expected and frames == 4
    overview = json.loads(request(GRAFANA + '/api/dashboards/uid/influxdb-replay', headers=AUTH))['dashboard']
    checks['overview_contains_new_section_and_link'] = any(UID in link.get('url', '') for link in overview.get('links', [])) and any('17.10.2026' in p.get('title', '') for p in overview['panels'] if p['type'] == 'row')
    result = {'passed': all(checks.values()), 'checks': checks, 'points': len(expected),
        'other_values_preserved': sum(s['points'] for s in before['history'].values()), 'other_series_preserved': len(before['history']),
        'rollback_width_samples': meta['rollback_width_samples'],
        'dashboard_url': GRAFANA + '/d/' + UID, 'source': 'Screenshot reconstruction; TPS unit requested by user, values unchanged',
        'start_msk': meta['start_msk'], 'end_msk': meta['end_msk']}
    save('verification.json', result)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
