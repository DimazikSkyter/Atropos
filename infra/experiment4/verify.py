"""Verify recorded real GC and response-time distributions. No third-party dependencies."""
import argparse
import bisect
import csv
import json
import math
from pathlib import Path
import re
import statistics


def percentile(values, p):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * p
    lo = int(position)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def describe(values):
    return {'count': len(values), 'mean_ms': statistics.fmean(values) if values else None,
            'sigma_ms': statistics.pstdev(values) if len(values) > 1 else None,
            'p50_ms': percentile(values, .5), 'p95_ms': percentile(values, .95),
            'p99_ms': percentile(values, .99)}


def correlation(a, b):
    if len(a) < 3 or len(a) != len(b):
        return None
    avg_a, avg_b = statistics.fmean(a), statistics.fmean(b)
    denom = math.sqrt(sum((x - avg_a) ** 2 for x in a) * sum((x - avg_b) ** 2 for x in b))
    return sum((x - avg_a) * (y - avg_b) for x, y in zip(a, b)) / denom if denom else None


def ratio(a, b):
    return a / b if a is not None and b is not None and b > 0 else None


def metric(text, name, contains=None):
    values = []
    for line in text.splitlines():
        if line.startswith('#'):
            continue
        if re.match(re.escape(name) + r'(?:\{|\s)', line) and (contains is None or contains in line):
            try:
                values.append(float(line.split()[-1]))
            except ValueError:
                pass
    return sum(values) if values else None


def verify(out):
    out = Path(out)
    metadata = json.loads((out / 'metadata.json').read_text(encoding='utf-8'))
    config = metadata['configuration']
    baseline = float(config.get('BASELINE_SECONDS', 180))
    ramp = float(config.get('RAMP_SECONDS', 900))
    hold = float(config.get('HOLD_SECONDS', 180))
    origin = float(metadata.get('experiment_start_uptime_ms', 0))
    total = baseline + ramp + hold
    def read_csv(name):
        file = out / name
        return list(csv.DictReader(file.open(encoding='utf-8'))) if file.exists() else []
    requests = []
    for row in read_csv('requests.csv'):
        requests.append({'start': float(row['start_ms']), 'end': float(row['end_ms']),
                         'ms': float(row['duration_ms'])})
    requests.sort(key=lambda row: row['start'])
    from gc_intervals import load_gc_intervals
    gc_data = load_gc_intervals(out)
    gc = gc_data['events']
    gc.sort(key=lambda row: row['start'])
    minor = [row for row in gc if row['kind'] == 'minor']
    minor_starts = [row['start'] for row in minor]
    def overlapping(req):
        index = bisect.bisect_right(minor_starts, req['end']) - 1
        return index >= 0 and minor[index]['end'] >= req['start'] and minor[index]['start'] <= req['end']
    def window(begin, end):
        rows = [row for row in requests if begin <= (row['start'] - origin) / 1000 < end]
        collections = [row for row in minor if begin <= (row['start'] - origin) / 1000 < end]
        hit = [row['ms'] for row in rows if overlapping(row)]
        miss = [row['ms'] for row in rows if not overlapping(row)]
        result = describe([row['ms'] for row in rows])
        result.update(begin_seconds=begin, end_seconds=end,
                      rps=len(rows) / (end - begin), minor_gc_per_second=len(collections) / (end - begin),
                      minor_gc_pause_seconds_per_second=sum(row['ms'] for row in collections) / 1000 / (end - begin),
                      gc_overlap_fraction=len(hit) / len(rows) if rows else None,
                      gc_overlap=describe(hit), no_gc_overlap=describe(miss))
        return result
    early = window(min(60, baseline / 3), baseline)
    late = window(baseline + ramp + max(0, hold - 120), total)
    size = 60 if ramp >= 300 else 15
    windows = [window(t, min(t + size, baseline + ramp))
               for t in range(int(baseline), int(baseline + ramp), size)]
    summary_path = out / 'k6-summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
    def k6_value(name, key, fallback=None):
        item = summary.get('metrics', {}).get(name, {})
        return item.get('values', item).get(key, fallback)
    records_path = out / 'metrics.ndjson'
    records = [json.loads(line) for line in records_path.read_text(encoding='utf-8').splitlines()] if records_path.exists() else []
    logged_metrics = [record.get('metrics', '') for record in records]
    current = logged_metrics[-1] if logged_metrics else ''
    durations = [row['ms'] for row in requests if 0 <= (row['start'] - origin) / 1000 < total]
    major = [row for row in gc if row['kind'] == 'major' and min(60, baseline / 3) <= (row['start'] - origin) / 1000 < total]
    measured_minor = [row for row in minor if min(60, baseline / 3) <= (row['start'] - origin) / 1000 < total]
    major_time = sum(row['ms'] for row in major)
    minor_time = sum(row['ms'] for row in measured_minor)
    sigma_ratio = ratio(late['sigma_ms'], early['sigma_ms'])
    gc_rate_ratio = ratio(late['minor_gc_per_second'], early['minor_gc_per_second'])
    pause_ratio = ratio(late['minor_gc_pause_seconds_per_second'], early['minor_gc_pause_seconds_per_second'])
    sigma_gc = correlation([row['minor_gc_pause_seconds_per_second'] for row in windows],
                           [row['sigma_ms'] or 0 for row in windows])
    ramp_time = [(row['begin_seconds'] + row['end_seconds']) / 2 for row in windows]
    ramp_gc_rate = [row['minor_gc_per_second'] for row in windows]
    time_gc = correlation(ramp_time, ramp_gc_rate)
    gc_slope = None
    if len(ramp_time) > 1:
        t_mean, gc_mean = statistics.fmean(ramp_time), statistics.fmean(ramp_gc_rate)
        gc_slope = sum((t - t_mean) * (g - gc_mean) for t, g in zip(ramp_time, ramp_gc_rate)) / sum((t - t_mean) ** 2 for t in ramp_time)
    overlap_delta = None
    if late['gc_overlap']['mean_ms'] is not None and late['no_gc_overlap']['mean_ms'] is not None:
        overlap_delta = late['gc_overlap']['mean_ms'] - late['no_gc_overlap']['mean_ms']
    dropped = k6_value('dropped_iterations', 'count', 0 if summary else None)
    failure_rate = k6_value('http_req_failed', 'rate')
    buckets = [line for line in current.splitlines() if line.startswith('exp4_request_duration_seconds_bucket{')]
    vm_path = out / 'vm-export.ndjson'
    vm_buckets = []
    if vm_path.exists():
        for line in vm_path.read_text(encoding='utf-8').splitlines():
            item = json.loads(line)
            if item.get('metric', {}).get('__name__') == 'exp4_request_duration_seconds_bucket' and item.get('values'):
                vm_buckets.append(item)
    elapsed = max((record.get('status', {}).get('elapsed_seconds', 0) for record in records), default=0)
    nominal_rps = float(config.get('LOAD_RPS', metadata.get('load_rps', 250)))
    timer_count = metric(current, 'exp4_request_duration_seconds_count')
    rps_ratio = ratio(late['rps'], early['rps'])
    log_dropped = metric(current, 'exp4_logs_dropped_total')
    throttle = [metric(text, 'exp4_cgroup_cpu_throttled_seconds_total') for text in logged_metrics]
    throttle = [value for value in throttle if value is not None]
    throttle_ratio = (throttle[-1] - throttle[0]) / max(elapsed, 1) if len(throttle) > 1 else None
    checks = {
        'completed_planned_duration': elapsed >= total - 10 and bool(durations),
        'at_least_15_minutes_or_explicit_pilot': total >= 900 or metadata.get('kind') == 'pilot',
        'k6_summary_present': bool(summary),
        'no_failed_http_requests': failure_rate == 0,
        'no_dropped_iterations': dropped == 0,
        'constant_completed_rps': rps_ratio is not None and .95 <= rps_ratio <= 1.05
                                  and late['rps'] >= .95 * nominal_rps,
        'micrometer_histogram_dense_buckets': len(buckets) >= 40,
        'histogram_stored_in_shared_victoriametrics': len(vm_buckets) >= 40,
        'raw_request_and_gc_logs_present': bool(requests) and bool(minor),
        'gc_clocks_independently_aligned': gc_data['clock_alignment']['valid'],
        'no_dropped_raw_logs': log_dropped == 0,
        'raw_timer_sample_counts_consistent': timer_count is not None and
                                             abs(timer_count - len(requests)) <= max(10 * nominal_rps, .01 * len(requests)),
        'minor_gc_dominates_pause_time': minor_time > 0 and major_time / (major_time + minor_time) < .1,
        'cpu_throttling_not_dominant': throttle_ratio is not None and throttle_ratio < .1,
    }
    if not metadata.get('control'):
        checks.update(minor_gc_frequency_increased=gc_rate_ratio is not None and gc_rate_ratio >= 2,
                      minor_gc_pause_duty_increased=pause_ratio is not None and pause_ratio >= 2,
                      gradual_minor_gc_trend=time_gc is not None and time_gc >= .8 and gc_slope > 0,
                      response_sigma_increased=sigma_ratio is not None and sigma_ratio >= 1.3,
                      gc_overlapping_requests_slower=overlap_delta is not None and overlap_delta >= .5)
    from bucket_analysis import analyze_buckets
    bucket_data = analyze_buckets(out)
    checks['micrometer_bucket_deltas_valid'] = bucket_data['valid']
    if not metadata.get('control'):
        checks['micrometer_distribution_changed'] = bucket_data['distribution_changed_tv_0_02']
    from analyze_modes import analyze_modes
    late_modes = analyze_modes([row['ms'] for row in requests
                                if late['begin_seconds'] <= (row['start'] - origin) / 1000 < late['end_seconds']])
    return {'passed': all(checks.values()), 'kind': metadata.get('kind'), 'control': metadata.get('control'),
            'checks': checks, 'duration_seconds': total, 'observed_elapsed_seconds': elapsed,
            'baseline': early, 'plateau': late, 'sigma_ratio': sigma_ratio,
            'minor_gc_frequency_ratio': gc_rate_ratio, 'minor_gc_pause_duty_ratio': pause_ratio,
            'ramp_sigma_gc_pause_correlation': sigma_gc,
            'minor_gc_rate_time_correlation': time_gc,
            'minor_gc_rate_ramp_slope_per_second_squared': gc_slope,
            'plateau_gc_overlap_mean_penalty_ms': overlap_delta,
            'plateau_modes': late_modes, 'major_gc_count': len(major),
            'gc_clock_alignment': gc_data['clock_alignment'],
            'micrometer_distribution': bucket_data,
            'major_gc_pause_ms': major_time, 'minor_gc_pause_ms': minor_time,
            'cpu_throttled_seconds_per_second': throttle_ratio,
            'k6_dropped_iterations': dropped, 'k6_http_failure_rate': failure_rate,
            'raw_request_count': len(requests), 'last_sampled_micrometer_timer_count': timer_count,
            'vm_histogram_bucket_series': len(vm_buckets), 'window_seconds': size,
            'ramp_windows': windows,
            'interpretation': 'GC overlap supports mechanism; observational correlation alone is not proof of exclusive causality. '
                              'Bimodality is reported separately and is not required for sigma expansion. '
                              'Pilot is calibration, not a completed >=15min experiment.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    args = parser.parse_args()
    report = verify(args.results)
    (args.results / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['passed'] else 1)
