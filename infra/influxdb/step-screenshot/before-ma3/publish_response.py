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


def read_expected(path=None):
    with (path or (ROOT / 'response-time.csv')).open(encoding='utf-8') as handle:
        return {(r['trace'], int(datetime.fromisoformat(r['timestamp']).timestamp())): float(r['value']) for r in csv.DictReader(handle)}


def local():
    _, _, checks = tps_verifier.local()
    meta = json.loads((ROOT / 'response-time-metadata.json').read_text(encoding='utf-8'))
    values = read_expected()
    times = sorted({t for _, t in values})
    checks.update({
        'tps_csv_and_lp_byte_identical': all((ROOT / name).read_bytes() == (ROOT / 'pre-install-backup/step-screenshot' / name).read_bytes() for name in ['metrics.csv', 'metrics.lp']),
        'vo_four_series_uniform_five_seconds': len(values) == meta['points'] == 47464 and all(sorted(t for s, t in values if s == trace) == times for trace in meta['traces']) and {b - a for a, b in zip(times, times[1:])} == {5},
        'vo_finite_within_display_scale': all(math.isfinite(v) and 0 <= v <= 1200 for v in values.values()),
        'vo_csv_hash_matches': hashlib.sha256((ROOT / 'response-time.csv').read_bytes()).hexdigest() == meta['csv_sha256'],
        'vo_lp_counts_no_crlf': len((ROOT / 'response-time.lp').read_bytes().splitlines()) == len(values) and b'\r' not in (ROOT / 'response-time.lp').read_bytes(),
        'reference_hash_matches': hashlib.sha256((ROOT / 'source/response-time-reference.png').read_bytes()).hexdigest() == meta['reference_sha256'],
        'small_step_events_in_both_tests': sorted(sum(e['phase'] == 'small_steps' and e['run'] == run['name'] for e in meta['incidents']) for run in meta['runs']) == [2, 3],
        'eight_tps_dips_drive_main_test_latency': sum(e['phase'] == 'all_scenarios' for e in meta['incidents']) == 8,
        'rare_period_changes_within_six_percent': .05 < sum(c['period_seconds'] != 360 for c in meta['cycles']) / len(meta['cycles']) < .2 and all(.94 <= c['period_seconds'] / 360 <= 1.06 for c in meta['cycles']),
        'rare_amplitude_changes_within_twelve_percent': .05 < sum(c['amplitude_ms'] != 4 for c in meta['cycles']) / len(meta['cycles']) < .3 and all(.88 <= c['amplitude_ms'] / 4 <= 1.12 for c in meta['cycles']),
    })
    for trace, base in meta['base_ms'].items():
        j = int(trace[-1]) - 1
        active = lambda t: any((j < 3 and r['boundaries'][j] <= t < r['boundaries'][j + 1]) or r['boundaries'][3] <= t < r['boundaries'][4] for r in meta['runs'])
        checks[trace + '_correct_scenario_gates'] = all((value > 0) == active(t) for (s, t), value in values.items() if s == trace)
        quiet = [v for (s, t), v in values.items() if s == trace and active(t) and not any(trace in e['traces'] and e['onset'] <= t <= e['end'] for e in meta['incidents'])]
        checks[trace + '_quiet_level_and_wave'] = abs(sum(quiet) / len(quiet) - base) < .2 and 5 < max(quiet) - min(quiet) < 12
    checks['latency_rises_during_actual_tps_troughs'] = all(values[s, e['trough']] - meta['base_ms'][s] > 70 for e in meta['incidents'] for s in e['traces'])
    checks['all_peaks_settle_after_tps_recovery'] = all(abs(values[s, e['end'] + 5] - meta['base_ms'][s]) < 6 for e in meta['incidents'] for s in e['traces'])
    old_values = read_expected(ROOT / 'previous-response/response-time.csv')
    checks['overwrites_exactly_previous_timestamps_and_tags'] = values.keys() == old_values.keys()
    checks['new_main_maxima_at_least_fifty_percent_higher'] = all(
        max(v for (s, t), v in values.items() if s == trace) > 1.5 * max(v for (s, t), v in old_values.items() if s == trace)
        for trace in meta['traces'])
    with (ROOT / 'metrics.csv').open(encoding='utf-8') as handle:
        tps = {int(datetime.fromisoformat(r['timestamp']).timestamp()): float(r['value']) for r in csv.DictReader(handle) if r['trace'] == 'orange'}
    metrics = []
    for event in meta['incidents']:
        for trace in event['traces']:
            base = meta['base_ms'][trace]
            points = [(t, values[trace, t] - base) for t in range(event['onset'], event['end'] + 1, 5) if values[trace, t] > 0]
            peak = max(y for t, y in points)
            high_width = sum(y > peak * .5 for t, y in points) * 5
            local_peaks = [t for t, y in points if event['onset'] + 15 < t < event['end'] - 15 and
                           y > values[trace, t - 5] - base and y >= values[trace, t + 5] - base and
                           y - min(values[trace, t - 15] - base, values[trace, t - 10] - base) > 6 and
                           y - min(values[trace, t + 15] - base, values[trace, t + 10] - base) > 6]
            core = [t for t, _ in points if t in tps and t <= event['recovery']]
            xs, ys = [tps[t] for t in core], [values[trace, t] for t in core]
            ax, ay = sum(xs) / len(xs), sum(ys) / len(ys)
            correlation = sum((a - ax) * (b - ay) for a, b in zip(xs, ys)) / math.sqrt(sum((a - ax) ** 2 for a in xs) * sum((b - ay) ** 2 for b in ys))
            metrics.append({'trace': trace, 'time_msk': event['time_msk'], 'phase': event['phase'],
                            'half_height_width_seconds': high_width, 'local_peaks': len(local_peaks),
                            'tps_vo_correlation_during_dip': round(correlation, 3), 'max_ms': round(base + peak, 3)})
    checks['every_episode_is_broad_not_one_sample'] = all(m['half_height_width_seconds'] >= 60 for m in metrics)
    checks['main_episodes_have_multiple_minor_peaks'] = all(m['local_peaks'] >= 2 for m in metrics if m['phase'] == 'all_scenarios')
    checks['vo_increases_as_tps_falls_in_each_episode'] = all(m['tps_vo_correlation_during_dip'] < -.5 for m in metrics)
    meta['episode_validation'] = metrics
    run2 = meta['runs'][1]
    checks['second_combined_phase_follows_quiet_tps'] = all(abs(v - meta['base_ms'][s]) < 6 for (s, t), v in values.items() if run2['boundaries'][3] <= t < run2['boundaries'][4])
    dashboard = json.loads((DASHBOARD_ROOT / 'dashboards' / (UID + '.json')).read_text(encoding='utf-8'))
    original = json.loads((ROOT / 'pre-install-backup/dashboard.json').read_text(encoding='utf-8'))
    checks['tps_panel_queries_and_style_preserved'] = all(all(p[k] == next(x for x in dashboard['panels'] if x['id'] == p['id'])[k] for k in p if k != 'gridPos') for p in original['panels'])
    checks['vo_raw_ms_panel_and_links'] = next(p for p in dashboard['panels'] if p['id'] == 7)['fieldConfig']['defaults']['unit'] == 'ms' and len(dashboard['links']) == 3 and not any('aggregateWindow' in t['query'] for p in dashboard['panels'] for t in p.get('targets', []))
    checks['pink_vertical_annotation_removed'] = not any(a.get('name') == 'Восстановление TPS' for a in dashboard['annotations']['list'])
    checks['higher_peaks_are_not_clipped_at_six_hundred'] = 'max' not in next(p for p in dashboard['panels'] if p['id'] == 7)['fieldConfig']['defaults']
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
    previous = live_values(parse((ROOT / 'previous-response/live-response.csv').read_bytes()))
    assert live_values(current) in (previous, expected), 'Live VO changed since backup'
    assert live_values(current).keys() == expected.keys(), 'Revision must replace the same points'
    # Replace exactly the same VO keys; leave TPS and other measurements alone.
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
    checks['grafana_pink_annotations_removed'] = not any(a.get('name') == 'Восстановление TPS' for a in dashboard['annotations']['list'])
    overview = json.loads(get(GRAFANA + '/api/dashboards/uid/influxdb-replay', headers=AUTH))['dashboard']
    checks['overview_includes_vo_panel'] = any(p['title'] == panel['title'] and p['targets'] == panel['targets'] for p in overview['panels'])
    result = {'passed': all(checks.values()), 'checks': checks, 'response_points': len(expected), 'preserved_tps_points': 15824,
              'latency_incidents': len(meta['incidents']), 'small_step_incidents': 5,
              'episode_validation': meta['episode_validation'], 'dashboard_url': GRAFANA + '/d/' + UID}
    save('response-time-verification.json', result)
    print(json.dumps({k: v for k, v in result.items() if k != 'episode_validation'}, indent=2))
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
