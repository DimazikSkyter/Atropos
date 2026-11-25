"""Model screenshot response times, keeping the digitized TPS samples unchanged."""
import csv
import hashlib
import json
import math
import random
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD_ROOT = ROOT if (ROOT / 'dashboards').is_dir() else ROOT.parent
UID = 'screenshot-steps-20261017'
MEASUREMENT = 'screenshot_response_time_replay'
MSK = timezone(timedelta(hours=3))
DS = {'type': 'influxdb', 'uid': 'influxdb-replay'}
BASES = {'bp1': 440, 'bp2': 410, 'bp3': 380, 'bp4': 360}
COLORS = {'bp1': '#9A7600', 'bp2': '#C15C17', 'bp3': '#0A8057', 'bp4': '#B4326C'}
PEAKS = {'bp1': 450, 'bp2': 330, 'bp3': 250, 'bp4': 210}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def get_runs(timestamps):
    # These are the same nearest pixel/timestamp boundaries as generate.py.
    ticks = [243, 546, 849, 1152, 1454, 1757, 2059, 2362]
    average_x = sum(ticks) / len(ticks)
    average_t = 3.5 * 7200
    slope = sum((i * 7200 - average_t) * (x - average_x) for i, x in enumerate(ticks)) / sum((i * 7200 - average_t) ** 2 for i in range(8))
    intercept = average_x - slope * average_t
    origin = datetime(2026, 10, 17, 16, tzinfo=MSK).timestamp()
    result = []
    for name, pixels in [('Основной тест', [575, 689, 791, 893, 1245]), ('Повторный тест', [1440, 1540, 1642, 1744, 2095])]:
        indices = [min(range(len(timestamps)), key=lambda i: abs(timestamps[i] - (origin + (x - intercept) / slope))) for x in pixels]
        result.append({'name': name, 'indices': indices, 'boundaries': [timestamps[i] for i in indices]})
    return result


def get_events(timestamps, orange, runs, tps_metadata):
    events = []
    for run in runs:
        start, _, _, high, end = run['indices']
        for phase, lo, hi, reference, cutoff in [
            ('small_steps', start + 3, high, 100.0, 3.0),
            ('all_scenarios', high + 12, end, tps_metadata['detail_calibration']['plateau_value'], 1.0),
        ]:
            candidates = []
            for i in range(lo, hi - 1):
                if orange[i] < reference - cutoff and orange[i] <= orange[i - 1] and orange[i] < orange[i + 1]:
                    if candidates and timestamps[i] - timestamps[candidates[-1]] < 120:
                        if orange[i] < orange[candidates[-1]]:
                            candidates[-1] = i
                    else:
                        candidates.append(i)
            for i in candidates:
                depth = reference - orange[i]
                threshold = reference - max(.45, depth * .12)
                left = right = i
                while left > lo and orange[left - 1] < threshold:
                    left -= 1
                while right + 1 < hi and orange[right + 1] < threshold:
                    right += 1
                recovery = min(right + 1, hi - 1)
                active = list(BASES) if phase == 'all_scenarios' else [
                    f'bp{j + 1}' for j in range(3) if run['boundaries'][j] <= timestamps[i] < run['boundaries'][j + 1]]
                assert active
                # The envelope follows the actual dip, not the later TPS overshoot.
                events.append({'run': run['name'], 'phase': phase, 'traces': active,
                               'onset': timestamps[left], 'trough': timestamps[i],
                               'recovery': timestamps[recovery], 'tps_trough': orange[i],
                               'tps_reference': reference, 'deficit': depth,
                               'deficit_curve': [[timestamps[j], max(0, (reference - orange[j]) / depth)]
                                                 for j in range(max(lo, left - 1), min(recovery + 2, hi))]})
    events.sort(key=lambda e: e['trough'])
    for i, event in enumerate(events):
        rng = random.Random(20261017 + i)
        event['amplitude_factor'] = rng.uniform(.90, 1.10)
        event['severity'] = .55 + .65 * math.sqrt(min(1, event['deficit'] / event['tps_reference'] / .06))
        event['tail_seconds'] = rng.choice([50, 55, 60])
        event['end'] = event['recovery'] + event['tail_seconds']
        width = event['recovery'] - event['onset']
        # Distinct broad retry clusters inside a continuous elevated envelope.
        count = max(3, min(5, round(width / 45)))
        event['retry_clusters'] = [
            {'timestamp': event['onset'] + width * ((j + 1) / (count + 1) + rng.uniform(-.025, .025)),
             'sigma_seconds': rng.uniform(7, 12), 'weight': rng.uniform(.75, 1.15)} for j in range(count)]
        event['time_msk'] = datetime.fromtimestamp(event['trough'], MSK).isoformat()
    return events


def smoothstep(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def deficit_at(stamp, event):
    curve = event['deficit_curve']
    if stamp <= curve[0][0] or stamp >= curve[-1][0]:
        return 0.0
    index = bisect_right([point[0] for point in curve], stamp) - 1
    (a, av), (b, bv) = curve[index:index + 2]
    return av + (bv - av) * (stamp - a) / (b - a)


def incident_pulse(stamp, trace, event):
    if trace not in event['traces'] or not event['onset'] <= stamp <= event['end']:
        return 0.0
    j = int(trace[-1]) - 1
    width = event['recovery'] - event['onset']
    rise = smoothstep((stamp - event['onset']) / min(25, width * .20))
    elapsed = max(0, stamp - event['recovery'])
    fall = math.exp(-elapsed / 30) * (1 - smoothstep(elapsed / event['tail_seconds']))
    # Requests already in flight keep latency raised as throughput recovers.
    stress = max(deficit_at(stamp, event), .7 * deficit_at(stamp, event) + .3 * deficit_at(stamp - 20, event))
    clusters = sum(c['weight'] * math.exp(-.5 * ((stamp - c['timestamp'] - j * 2.5) / (c['sigma_seconds'] * (1 + j * .04))) ** 2)
                   for c in event['retry_clusters'])
    height = PEAKS[trace] * event['amplitude_factor'] * event['severity'] * (1 + .035 * math.sin(j + event['trough']))
    return height * rise * fall * (.12 + .56 * stress ** .65 + .36 * clusters)


def make_cycles(start, end):
    rng = random.Random(20261017)
    cycles, current = [], start
    while current <= end:
        # A rare cycle has a different length; phase stays continuous at joins.
        period = 360 * (1 + rng.uniform(-.06, .06)) if rng.random() < .12 else 360.0
        amplitude = 4 * (1 + rng.uniform(-.12, .12)) if rng.random() < .18 else 4.0
        cycles.append({'start': current, 'period_seconds': period, 'amplitude_ms': amplitude})
        current += period
    return cycles


def modeled_values(timestamps, runs, events, cycles):
    rng = random.Random(440410380360)
    values, baseline, pulses = {s: [] for s in BASES}, {s: [] for s in BASES}, {s: [] for s in BASES}
    cycle_index = 0
    for stamp in timestamps:
        while cycle_index + 1 < len(cycles) and stamp >= cycles[cycle_index + 1]['start']:
            cycle_index += 1
        cycle = cycles[cycle_index]
        phase = 2 * math.pi * (stamp - cycle['start']) / cycle['period_seconds']
        for j, (trace, base) in enumerate(BASES.items()):
            active = any((j < 3 and r['boundaries'][j] <= stamp < r['boundaries'][j + 1]) or
                         r['boundaries'][3] <= stamp < r['boundaries'][4] for r in runs)
            wave = cycle['amplitude_ms'] * (1 - j * .04) * math.sin(phase + .13 * j)
            quiet = base + wave + rng.uniform(-.8, .8) if active else 0.0
            pulse = 0.0
            if active:
                for event in events:
                    pulse += incident_pulse(stamp, trace, event)
            baseline[trace].append(round(quiet, 3))
            pulses[trace].append(round(pulse, 3))
            values[trace].append(round(quiet + pulse, 3))
    return values, baseline, pulses


def make_panel(description):
    targets, overrides = [], []
    for ref, (trace, base) in zip('ABCD', BASES.items()):
        targets.append({'refId': ref, 'datasource': DS, 'query':
            'import "date"\n\nfrom(bucket: v.defaultBucket)\n'
            '  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))\n'
            f'  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "value" and r.trace == "{trace}")\n'
            '  |> keep(columns: ["_time", "_value"])\n  |> group(columns: [])\n  |> sort(columns: ["_time"])'})
        overrides.append({'matcher': {'id': 'byFrameRefID', 'options': ref}, 'properties': [
            {'id': 'displayName', 'value': f'БП {trace[-1]} · {base} мс'},
            {'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': COLORS[trace]}}]})
    return {'id': 7, 'type': 'timeseries', 'title': 'ВО · время отклика БП 1–4', 'description': description,
            'datasource': DS, 'gridPos': {'x': 0, 'y': 16, 'w': 24, 'h': 14},
            'interval': '5s', 'maxDataPoints': 50000, 'targets': targets,
            'fieldConfig': {'defaults': {'unit': 'ms', 'decimals': 1, 'min': 0,
                'custom': {'drawStyle': 'line', 'lineInterpolation': 'linear', 'lineWidth': 1.8,
                           'fillOpacity': 0, 'showPoints': 'never', 'spanNulls': False,
                           'axisPlacement': 'left', 'axisLabel': 'ВО, мс', 'axisSoftMax': 1200,
                           'scaleDistribution': {'type': 'linear'}, 'stacking': {'mode': 'none', 'group': 'A'}}},
                            'overrides': overrides},
            'options': {'legend': {'showLegend': True, 'displayMode': 'list', 'placement': 'bottom', 'calcs': []},
                        'tooltip': {'mode': 'multi', 'sort': 'desc'}}}


def build():
    with (ROOT / 'metrics.csv').open(encoding='utf-8') as handle:
        orange_rows = [r for r in csv.DictReader(handle) if r['trace'] == 'orange']
    tps_timestamps = [int(datetime.fromisoformat(r['timestamp']).timestamp()) for r in orange_rows]
    orange = [float(r['value']) for r in orange_rows]
    tps_metadata = json.loads((ROOT / 'metadata.json').read_text(encoding='utf-8'))
    runs = get_runs(tps_timestamps)
    events = get_events(tps_timestamps, orange, runs, tps_metadata)
    timestamps = list(range(tps_timestamps[0], tps_timestamps[-1] + 1, 5))
    cycles = make_cycles(timestamps[0], timestamps[-1])
    values, baseline, pulses = modeled_values(timestamps, runs, events, cycles)
    with (ROOT / 'response-time.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['timestamp', 'trace', 'value'])
        for i, stamp in enumerate(timestamps):
            for trace in BASES:
                writer.writerow([datetime.fromtimestamp(stamp, MSK).isoformat(), trace, values[trace][i]])
    lp = [f'{MEASUREMENT},trace={trace} value={values[trace][i]:.3f} {stamp}' for i, stamp in enumerate(timestamps) for trace in BASES]
    (ROOT / 'response-time.lp').write_bytes(('\n'.join(lp) + '\n').encode())
    description = ('ВО БП 1–4: 440/410/380/360 мс. Модель по эскизу пользователя, не исходная телеметрия. '
                   'Оба теста: сначала БП 1, 2, 3 по отдельности, затем все четыре. '
                   'Широкие всплески ВО следуют всей длительности провалов TPS, с несколькими локальными пиками '
                   'и плавным затуханием после восстановления. Учтены также затупы малых шагов обоих тестов. '
                   'Базовая волна около 6 минут, амплитуда около 4 мс; '
                   'в 12% циклов период отклоняется до ±6%, в 18% — амплитуда до ±12%, шум ±0,8 мс. '
                   'Участки без зарегистрированных затупов TPS сохраняют спокойный ВО. Шаг 5 с, без усреднения.')
    dashboard_path = DASHBOARD_ROOT / 'dashboards' / (UID + '.json')
    dashboard = json.loads(dashboard_path.read_text(encoding='utf-8'))
    if not any(p['id'] == 7 for p in dashboard['panels']):
        for panel in dashboard['panels']:
            if panel['gridPos']['y'] >= 16:
                panel['gridPos']['y'] += 14
        dashboard['panels'].insert(1, make_panel(description))
    else:
        dashboard['panels'] = [make_panel(description) if p['id'] == 7 else p for p in dashboard['panels']]
    dashboard['description'] = tps_metadata['description'] + '\n\n' + description
    dashboard['annotations']['list'] = [a for a in dashboard['annotations']['list'] if a.get('name') != 'Восстановление TPS']
    dashboard['version'] = 4
    for run in runs:
        start, *_, end = run['boundaries']
        link = {'type': 'link', 'title': run['name'] + ' · ВО',
                'url': f'/d/{UID}?from={(start - 120) * 1000}&to={(end + 120) * 1000}&timezone=Europe%2FMoscow&viewPanel=panel-7', 'targetBlank': False}
        dashboard['links'] = [x for x in dashboard.get('links', []) if x.get('title') != link['title']] + [link]
    save(dashboard_path, dashboard)
    metadata = {'revision': 2, 'measurement': MEASUREMENT, 'field': 'value', 'tag': 'trace', 'traces': list(BASES),
                'units': 'ms', 'base_ms': BASES, 'colors': COLORS, 'interval_seconds': 5,
                'points_per_trace': len(timestamps), 'points': len(lp), 'is_raw_telemetry': False,
                'description': description, 'runs': runs, 'incidents': events,
                'shape_model': 'TPS-deficit envelope with multiple retry clusters and a recovery tail',
                'peak_excess_scale_ms': PEAKS,
                'cycles': cycles, 'period_change_probability': .12, 'period_max_relative_change': .06,
                'amplitude_change_probability': .18, 'amplitude_max_relative_change': .12,
                'tps_csv_sha256': hashlib.sha256((ROOT / 'metrics.csv').read_bytes()).hexdigest(),
                'csv_sha256': hashlib.sha256((ROOT / 'response-time.csv').read_bytes()).hexdigest(),
                'reference_sha256': hashlib.sha256((ROOT / 'source/response-time-reference.png').read_bytes()).hexdigest(),
                'feedback_sources': {name: hashlib.sha256((ROOT / 'source' / name).read_bytes()).hexdigest()
                                     for name in ['response-thin-peaks-feedback.png', 'tps-dips-feedback.png']},
                'seed': 20261017}
    save(ROOT / 'response-time-metadata.json', metadata)
    tps_metadata['response_time'] = {k: v for k, v in metadata.items() if k not in {'cycles', 'incidents'}}
    save(ROOT / 'metadata.json', tps_metadata)
    render(timestamps, values, runs, events)
    render_correlation(timestamps, values, tps_timestamps, orange)
    print(json.dumps({'response_points': len(lp), 'latency_incidents': len(events),
                      'small_step_recoveries': sum(e['phase'] == 'small_steps' for e in events)}))
    return metadata, baseline, pulses


def render(timestamps, values, runs, events):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    fig, axes = plt.subplots(2, 1, figsize=(16, 9), layout='constrained')
    dates = [datetime.fromtimestamp(t, MSK) for t in timestamps]
    for axis, run in zip(axes, runs):
        start, b, c, high, end = run['boundaries']
        for trace, base in BASES.items():
            axis.plot(dates, values[trace], lw=1.1, color=COLORS[trace], label=f'БП {trace[-1]} · {base} мс')
        for left, right, label in zip(run['boundaries'][:-1], run['boundaries'][1:], ['Сценарий 1', 'Сценарий 2', 'Сценарий 3', 'Все 4 сценария']):
            x1, x2 = datetime.fromtimestamp(left, MSK), datetime.fromtimestamp(right, MSK)
            axis.axvline(x1, color='#7a97b3', ls='--', lw=.7, alpha=.6)
            axis.text(x1 + (x2 - x1) / 2, 1100, label, ha='center', fontsize=10)
        axis.set(xlim=(datetime.fromtimestamp(start - 120, MSK), datetime.fromtimestamp(end + 120, MSK)), ylim=(0, 1200), ylabel='ВО, мс')
        axis.set_title(run['name'] + ' · 17–18.10.2026, Москва', loc='right', fontsize=12)
        axis.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=MSK))
        axis.grid(alpha=.25)
        axis.legend(loc='lower left', bbox_to_anchor=(0, 1.01), ncol=4, fontsize=10)
    fig.savefig(ROOT / 'response-time-preview.png', dpi=125)
    plt.close(fig)


def render_correlation(timestamps, values, tps_timestamps, orange):
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    fig, axes = plt.subplots(2, 1, figsize=(16, 8), sharex=True, layout='constrained', gridspec_kw={'height_ratios': [1, 2]})
    axes[0].plot([datetime.fromtimestamp(t, MSK) for t in tps_timestamps], orange, color='#FF9830', lw=1.5)
    axes[0].set(ylabel='TPS', ylim=(150, 182), title='Провалы TPS и широкие эпизоды повышенного ВО · основной тест')
    dates = [datetime.fromtimestamp(t, MSK) for t in timestamps]
    for trace, base in BASES.items():
        axes[1].plot(dates, values[trace], color=COLORS[trace], lw=1.3, label=f'БП {trace[-1]} · {base} мс')
    axes[1].set(ylabel='ВО, мс', ylim=(300, 1100), xlim=(datetime(2026, 10, 17, 20, 20, tzinfo=MSK), datetime(2026, 10, 17, 22, 10, tzinfo=MSK)))
    axes[1].legend(ncol=4, loc='upper left')
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=MSK))
    for axis in axes:
        axis.grid(alpha=.2)
    fig.savefig(ROOT / 'response-correlation-preview.png', dpi=125)
    plt.close(fig)


if __name__ == '__main__':
    build()
