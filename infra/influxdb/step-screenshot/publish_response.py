"""Validate, publish only the new VO measurement, then verify through Grafana."""
import argparse
import csv
import hashlib
import json
import math
import statistics
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


def autocorrelation(values, lag):
    mean = statistics.fmean(values)
    return sum((a - mean) * (b - mean) for a, b in zip(values[:-lag], values[lag:])) / sum((v - mean) ** 2 for v in values)


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
        'rare_amplitude_changes_within_twelve_percent': .05 < sum(c['amplitude_ms'] != meta['wave_amplitude_ms'] for c in meta['cycles']) / len(meta['cycles']) < .3 and all(.88 <= c['amplitude_ms'] / meta['wave_amplitude_ms'] <= 1.12 for c in meta['cycles']),
        'components_hash_matches': hashlib.sha256((ROOT / 'response-time-components.csv').read_bytes()).hexdigest() == meta['components_sha256'],
        'ma_process_is_order_three': meta['ma3_order'] == 3 and len(meta['ma3_coefficients']) == 4 and meta['ma3_coefficients'][3] != 0,
        'ma_sigma_configured_at_most_one_hundred': all(0 < s <= 100 for s in meta['ma3_sigma_ms'].values()),
        'deterministic_retry_waves_removed': all('retry_clusters' not in e for e in meta['incidents']),
    })
    with (ROOT / 'response-time-components.csv').open(encoding='utf-8') as handle:
        components = {(row['trace'], int(row['epoch'])): {k: float(v) for k, v in row.items() if k not in {'trace', 'epoch'}} for row in csv.DictReader(handle)}
    checks['all_output_values_use_recorded_components'] = components.keys() == values.keys() and all(
        abs(value - (meta['base_ms'][s] * c['active'] + c['wave_ms'] + c['background_noise_ms'] + c['incident_mean_ms'] + c['ma3_applied_ms'])) < .00051
        for (s, t), value in values.items() for c in [components[s, t]])
    statistical_validation = {}
    for trace, base in meta['base_ms'].items():
        j = int(trace[-1]) - 1
        active = lambda t: any((j < 3 and r['boundaries'][j] <= t < r['boundaries'][j + 1]) or r['boundaries'][3] <= t < r['boundaries'][4] for r in meta['runs'])
        checks[trace + '_correct_scenario_gates'] = all((value > 0) == active(t) for (s, t), value in values.items() if s == trace)
        quiet_times = [t for t in times if active(t) and components[trace, t]['incident_gate'] == 0]
        quiet = [values[trace, t] for t in quiet_times]
        quiet_residual = [values[trace, t] - base - components[trace, t]['wave_ms'] for t in quiet_times]
        background_sd = statistics.pstdev(quiet_residual)
        checks[trace + '_background_sigma_five_to_ten_ms'] = 5 <= background_sd <= 10 and abs(statistics.fmean(quiet_residual)) < .6
        checks[trace + '_quiet_level'] = abs(statistics.fmean(quiet) - base) < .7
        ma_raw = [components[trace, t]['ma3_raw_ms'] for t in times]
        ma_sd = statistics.pstdev(ma_raw)
        correlations = [autocorrelation(ma_raw, lag) for lag in range(1, 13)]
        checks[trace + '_ma_stationary_sigma_matches_target'] = abs(ma_sd / meta['ma3_sigma_ms'][trace] - 1) < .05
        checks[trace + '_ma_acf_cuts_off_after_lag_three'] = correlations[0] > .45 and correlations[1] > .15 and correlations[2] > .04 and max(abs(r) for r in correlations[3:]) < .05
        core_times = [t for t in times if active(t) and components[trace, t]['incident_gate'] == 1]
        actual_core = [values[trace, t] - base - components[trace, t]['background_noise_ms'] - components[trace, t]['incident_mean_ms'] for t in core_times]
        checks[trace + '_burst_sigma_matches_ma_process'] = .8 < statistics.pstdev(actual_core) / meta['ma3_sigma_ms'][trace] < 1.2
        checks[trace + '_no_periodic_wave_inside_bursts'] = all(components[trace, t]['wave_ms'] == 0 for t in core_times)
        checks[trace + '_burst_mean_is_elevated'] = statistics.fmean(values[trace, t] for t in core_times) > base + 50
        # Detect the slow cycle from actual noisy output on a quiet interval.
        start = int(datetime.fromisoformat('2026-10-17T22:00:00+03:00').timestamp())
        stop = start + 1800
        sample = [(t - start, values[trace, t]) for t in times if start <= t < stop]
        mean = statistics.fmean(v for _, v in sample)
        spectrum = [(2 * math.hypot(sum((v - mean) * math.cos(2 * math.pi * t / period) for t, v in sample),
                                   sum((v - mean) * math.sin(2 * math.pi * t / period) for t, v in sample)) / len(sample), period)
                    for period in range(240, 481, 5)]
        amplitude, period = max(spectrum)
        checks[trace + '_visible_cycle_near_six_minutes'] = 330 <= period <= 390 and 5 < amplitude < 12
        statistical_validation[trace] = {'background_sigma_ms': round(background_sd, 3), 'ma3_stationary_sigma_ms': round(ma_sd, 3),
                                        'ma3_sigma_during_bursts_ms': round(statistics.pstdev(actual_core), 3),
                                        'ma3_acf_lags_1_to_12': [round(r, 4) for r in correlations],
                                        'detected_quiet_cycle_seconds': period, 'detected_quiet_amplitude_ms': round(amplitude, 3)}
    checks['independent_ma3_between_business_processes'] = all(
        abs(statistics.correlation([components[a, t]['ma3_raw_ms'] for t in times], [components[b, t]['ma3_raw_ms'] for t in times])) < .05
        for a, b in [('bp1', 'bp2'), ('bp1', 'bp3'), ('bp1', 'bp4'), ('bp2', 'bp3'), ('bp2', 'bp4'), ('bp3', 'bp4')])
    checks['ma3_applied_only_to_incidents'] = all(c['ma3_applied_ms'] == 0 for c in components.values() if c['incident_gate'] == 0)
    old_values = read_expected(ROOT / 'before-ma3/response-time.csv')
    checks['overwrites_exactly_previous_timestamps_and_tags'] = values.keys() == old_values.keys()
    checks['maxima_remain_near_previous_levels'] = all(
        .8 < max(v for (s, t), v in values.items() if s == trace) / max(v for (s, t), v in old_values.items() if s == trace) < 1.2
        for trace in meta['traces'])
    metrics = []
    for event in meta['incidents']:
        for trace in event['traces']:
            base = meta['base_ms'][trace]
            points = [(t, values[trace, t] - base) for t in range(event['onset'], event['end'] + 1, 5) if values[trace, t] > 0]
            peak = max(y for t, y in points)
            metrics.append({'trace': trace, 'time_msk': event['time_msk'], 'phase': event['phase'],
                            'samples_above_base_plus_fifty_ms': sum(y > 50 for t, y in points), 'max_ms': round(base + peak, 3)})
    checks['every_incident_has_many_elevated_samples'] = all(m['samples_above_base_plus_fifty_ms'] >= 4 for m in metrics)
    meta['episode_validation'] = metrics
    meta['statistical_validation'] = statistical_validation
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
    previous = live_values(parse((ROOT / 'before-ma3/live-response.csv').read_bytes()))
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
              'episode_validation': meta['episode_validation'], 'statistical_validation': meta['statistical_validation'], 'dashboard_url': GRAFANA + '/d/' + UID}
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
