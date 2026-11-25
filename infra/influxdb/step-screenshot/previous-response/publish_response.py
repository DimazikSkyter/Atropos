"""Validate, publish only the new VO measurement, then verify through Grafana."""
import argparse
import csv
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

from capture_live import AUTH, TOKEN, flux, get, parse
import publish_verify as tps_verifier

ROOT = Path(__file__).resolve().parent
DASHBOARD_ROOT = ROOT if (ROOT / 'dashboards').is_dir() else ROOT.parent
MEASUREMENT = 'screenshot_response_time_replay'
UID = 'screenshot-steps-20261017'
GRAFANA = 'http://127.0.0.1:3001'


def save(name, value):
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def read_expected():
    with (ROOT / 'response-time.csv').open(encoding='utf-8') as handle:
        return {(r['trace'], int(datetime.fromisoformat(r['timestamp']).timestamp())): float(r['value']) for r in csv.DictReader(handle)}


def local():
    _, _, checks = tps_verifier.local()
    meta = json.loads((ROOT / 'response-time-metadata.json').read_text(encoding='utf-8'))
    values = read_expected()
    times = sorted({t for _, t in values})
    checks.update({
        'tps_csv_and_lp_byte_identical': all((ROOT / name).read_bytes() == (ROOT / 'pre-install-backup/step-screenshot' / name).read_bytes() for name in ['metrics.csv', 'metrics.lp']),
        'vo_four_series_uniform_five_seconds': len(values) == meta['points'] == 47464 and all(sorted(t for s, t in values if s == trace) == times for trace in meta['traces']) and {b - a for a, b in zip(times, times[1:])} == {5},
        'vo_finite_within_display_scale': all(math.isfinite(v) and 0 <= v <= 600 for v in values.values()),
        'vo_csv_hash_matches': hashlib.sha256((ROOT / 'response-time.csv').read_bytes()).hexdigest() == meta['csv_sha256'],
        'vo_lp_counts_no_crlf': len((ROOT / 'response-time.lp').read_bytes().splitlines()) == len(values) and b'\r' not in (ROOT / 'response-time.lp').read_bytes(),
        'reference_hash_matches': hashlib.sha256((ROOT / 'source/response-time-reference.png').read_bytes()).hexdigest() == meta['reference_sha256'],
        'small_step_events_in_both_tests': sorted(sum(e['phase'] == 'small_steps' and e['run'] == run['name'] for e in meta['recovery_events']) for run in meta['runs']) == [2, 3],
        'eight_recovery_peaks_from_main_test': sum(e['phase'] == 'all_scenarios' for e in meta['recovery_events']) == 8,
        'rare_period_changes_within_six_percent': .05 < sum(c['period_seconds'] != 360 for c in meta['cycles']) / len(meta['cycles']) < .2 and all(.94 <= c['period_seconds'] / 360 <= 1.06 for c in meta['cycles']),
        'rare_amplitude_changes_within_twelve_percent': .05 < sum(c['amplitude_ms'] != 4 for c in meta['cycles']) / len(meta['cycles']) < .3 and all(.88 <= c['amplitude_ms'] / 4 <= 1.12 for c in meta['cycles']),
    })
    for trace, base in meta['base_ms'].items():
        j = int(trace[-1]) - 1
        active = lambda t: any((j < 3 and r['boundaries'][j] <= t < r['boundaries'][j + 1]) or r['boundaries'][3] <= t < r['boundaries'][4] for r in meta['runs'])
        checks[trace + '_correct_scenario_gates'] = all((value > 0) == active(t) for (s, t), value in values.items() if s == trace)
        quiet = [v for (s, t), v in values.items() if s == trace and active(t) and not any(trace in e['traces'] and -15 <= t - e['timestamp'] <= e['tail_seconds'] + 5 for e in meta['recovery_events'])]
        checks[trace + '_quiet_level_and_wave'] = abs(sum(quiet) / len(quiet) - base) < .2 and 5 < max(quiet) - min(quiet) < 12
    checks['all_recoveries_have_vo_peaks'] = all(values[s, e['timestamp']] - meta['base_ms'][s] > (10 if s in {'bp3', 'bp4'} else 25) for e in meta['recovery_events'] for s in e['traces'])
    checks['all_peaks_settle_within_sixty_seconds'] = all(abs(values[s, e['timestamp'] + 65] - meta['base_ms'][s]) < 6 for e in meta['recovery_events'] for s in e['traces'])
    run2 = meta['runs'][1]
    checks['second_combined_phase_follows_quiet_tps'] = all(abs(v - meta['base_ms'][s]) < 6 for (s, t), v in values.items() if run2['boundaries'][3] <= t < run2['boundaries'][4])
    dashboard = json.loads((DASHBOARD_ROOT / 'dashboards' / (UID + '.json')).read_text(encoding='utf-8'))
    original = json.loads((ROOT / 'pre-install-backup/dashboard.json').read_text(encoding='utf-8'))
    checks['tps_panel_queries_and_style_preserved'] = all(all(p[k] == next(x for x in dashboard['panels'] if x['id'] == p['id'])[k] for k in p if k != 'gridPos') for p in original['panels'])
    checks['vo_raw_ms_panel_and_links'] = next(p for p in dashboard['panels'] if p['id'] == 7)['fieldConfig']['defaults']['unit'] == 'ms' and len(dashboard['links']) == 3 and not any('aggregateWindow' in t['query'] for p in dashboard['panels'] for t in p.get('targets', []))
    assert all(checks.values()), {k: v for k, v in checks.items() if not v}
    return meta, values, checks


def query_rows(measurement):
    return parse(flux('from(bucket: "metrics-replay") |> range(start: 0, stop: 2100-01-01T00:00:00Z) '
                      f'|> filter(fn: (r) => r._measurement == "{measurement}")'))


def live_values(rows):
    return {(r['trace'], int(datetime.fromisoformat(r['_time'].replace('Z', '+00:00')).timestamp())): float(r['_value']) for r in rows}


def publish(meta, expected):
    old = parse((ROOT / 'pre-install-backup/live-replay.csv').read_bytes())
    assert live_values(query_rows('screenshot_step_replay')) == live_values(old), 'TPS changed since snapshot'
    current = query_rows(MEASUREMENT)
    assert not current or live_values(current) == expected, 'New measurement has unexpected existing data'
    # Exactly one new measurement, no delete API and no writes to existing TPS.
    get('http://127.0.0.1:8086/api/v2/write?org=atropos&bucket=metrics-replay&precision=s',
        (ROOT / 'response-time.lp').read_bytes(), {'Authorization': 'Token ' + TOKEN, 'Content-Type': 'text/plain; charset=utf-8'})
    print(json.dumps({'written_response_points': len(expected), 'measurement': MEASUREMENT}))


def grafana_query(panel, time_range):
    payload = {'queries': [{**target, 'intervalMs': 5000 if panel['id'] == 7 else 15000, 'maxDataPoints': 50000} for target in panel['targets']]}
    payload.update({key: str(int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)) for key, value in time_range.items()})
    return json.loads(get(GRAFANA + '/api/ds/query', json.dumps(payload).encode(), {**AUTH, 'Content-Type': 'application/json'}))['results']


def verify_live(meta, expected, checks):
    rows = query_rows(MEASUREMENT)
    checks['influx_all_vo_values_exactly_match'] = len(rows) == len(expected) and live_values(rows) == expected and {r['_field'] for r in rows} == {'value'}
    checks['influx_existing_tps_unchanged'] = live_values(query_rows('screenshot_step_replay')) == live_values(parse((ROOT / 'pre-install-backup/live-replay.csv').read_bytes()))
    dashboard = json.loads(get(GRAFANA + '/api/dashboards/uid/' + UID, headers=AUTH))['dashboard']
    local_dashboard = json.loads((DASHBOARD_ROOT / 'dashboards' / (UID + '.json')).read_text(encoding='utf-8'))
    checks['grafana_live_panels_and_annotation_match_files'] = all(dashboard[key] == local_dashboard[key] for key in ['panels', 'links', 'time', 'timezone', 'annotations'])
    rendered = {}
    panel = next(p for p in dashboard['panels'] if p['id'] == 7)
    results = grafana_query(panel, dashboard['time'])
    for ref, trace in zip('ABCD', meta['traces']):
        assert not results[ref].get('error'), results[ref]
        for frame in results[ref].get('frames', []):
            fields, data = frame['schema']['fields'], frame['data']['values']
            ti = next(i for i, f in enumerate(fields) if f['type'] == 'time')
            vi = next(i for i, f in enumerate(fields) if f['type'] == 'number')
            rendered.update(((trace, int(t / 1000)), v) for t, v in zip(data[ti], data[vi]))
    checks['grafana_all_four_vo_series_exactly_match'] = rendered == expected
    annotation = dashboard['annotations']['list'][0]
    result = grafana_query({'id': 7, 'targets': [annotation['target']]}, dashboard['time'])['Anno']
    checks['grafana_annotation_query_returns_all_thirteen_regions'] = not result.get('error') and sum(len(f['data']['values'][0]) for f in result.get('frames', [])) == 13
    overview = json.loads(get(GRAFANA + '/api/dashboards/uid/influxdb-replay', headers=AUTH))['dashboard']
    checks['overview_includes_vo_panel'] = any(p['title'] == panel['title'] and p['targets'] == panel['targets'] for p in overview['panels'])
    result = {'passed': all(checks.values()), 'checks': checks, 'response_points': len(expected), 'preserved_tps_points': 15824,
              'recovery_events': len(meta['recovery_events']), 'small_step_recovery_events': 5, 'dashboard_url': GRAFANA + '/d/' + UID}
    save('response-time-verification.json', result)
    print(json.dumps(result, indent=2))
    assert result['passed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['draft', 'write', 'after'])
    phase = parser.parse_args().phase
    meta, expected, checks = local()
    if phase == 'draft':
        save('response-time-draft-verification.json', {'passed': True, 'checks': checks})
        print(json.dumps({'passed': True, 'checks': checks}, indent=2))
    elif phase == 'write':
        publish(meta, expected)
    else:
        verify_live(meta, expected, checks)


if __name__ == '__main__':
    main()
