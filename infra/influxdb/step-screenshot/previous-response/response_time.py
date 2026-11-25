"""Model screenshot response times, keeping the digitized TPS samples unchanged."""
import csv
import hashlib
import json
import math
import random
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
PEAKS = {'bp1': 65, 'bp2': 45, 'bp3': 24, 'bp4': 18}


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
        candidates = []
        for i in range(start + 3, high - 1):
            # Only recorded TPS dips; do not invent slowdowns in quiet sections.
            if orange[i] < 97 and orange[i] <= orange[i - 1] and orange[i] < orange[i + 1]:
                if candidates and timestamps[i] - timestamps[candidates[-1]] < 120:
                    if orange[i] < orange[candidates[-1]]:
                        candidates[-1] = i
                else:
                    candidates.append(i)
        for i in candidates:
            recovery = next((j for j in range(i + 1, min(i + 41, high)) if orange[j] >= 99), None)
            assert recovery is not None, ('No TPS recovery found', i)
            stamp = timestamps[recovery]
            active = [f'bp{j + 1}' for j in range(3) if run['boundaries'][j] <= stamp < run['boundaries'][j + 1]]
            assert len(active) == 1
            events.append({'run': run['name'], 'phase': 'small_steps', 'traces': active,
                           'trough': timestamps[i], 'timestamp': stamp, 'tps_trough': orange[i]})
        # Exact recovery peaks digitized from the detailed main-test screenshot.
        for turn in tps_metadata['turns']:
            stamp = int(datetime.fromisoformat(turn['timestamp_msk']).timestamp())
            if turn['sign'] > 0 and timestamps[high] <= stamp < timestamps[end]:
                events.append({'run': run['name'], 'phase': 'all_scenarios', 'traces': list(BASES),
                               'timestamp': stamp, 'tps_peak': turn['value']})
    for i, event in enumerate(sorted(events, key=lambda e: e['timestamp'])):
        event['amplitude_factor'] = random.Random(20261017 + i).uniform(.88, 1.10)
        event['tail_seconds'] = random.Random(17 + i).choice([40, 45, 50, 55, 60])
        event['time_msk'] = datetime.fromtimestamp(event['timestamp'], MSK).isoformat()
    return sorted(events, key=lambda e: e['timestamp'])


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
                    delta = stamp - event['timestamp']
                    if trace not in event['traces'] or not -10 <= delta <= event['tail_seconds']:
                        continue
                    height = PEAKS[trace] * event['amplitude_factor']
                    pulse += height * math.exp(-.5 * (delta / 3.5) ** 2)
                    if delta > 0:
                        # Two damped oscillations, fading fully within 40–60 s.
                        window = (1 - delta / event['tail_seconds']) ** 2
                        pulse += height * .16 * math.exp(-delta / 24) * math.sin(4 * math.pi * delta / event['tail_seconds']) * window
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
            'fieldConfig': {'defaults': {'unit': 'ms', 'decimals': 1, 'min': 0, 'max': 600,
                'custom': {'drawStyle': 'line', 'lineInterpolation': 'linear', 'lineWidth': 1.5,
                           'fillOpacity': 0, 'showPoints': 'never', 'spanNulls': False,
                           'axisPlacement': 'left', 'axisLabel': 'ВО, мс',
                           'scaleDistribution': {'type': 'linear'}, 'stacking': {'mode': 'none', 'group': 'A'}}},
                            'overrides': overrides},
            'options': {'legend': {'showLegend': True, 'displayMode': 'list', 'placement': 'bottom', 'calcs': []},
                        'tooltip': {'mode': 'multi', 'sort': 'desc'}}}


def recovery_annotation(events):
    rows = []
    for event in events:
        iso = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace('+00:00', 'Z')
        text = event['run'] + ' · восстановление TPS · ' + ('малый шаг' if event['phase'] == 'small_steps' else 'все 4 сценария')
        rows.append('{time: time(v: ' + json.dumps(iso(event['timestamp'])) + '), timeEnd: time(v: ' +
                    json.dumps(iso(event['timestamp'] + 30)) + '), text: ' + json.dumps(text, ensure_ascii=False) + '}')
    return {'name': 'Восстановление TPS', 'datasource': DS, 'enable': True, 'hide': False,
            'iconColor': '#B4326C', 'filter': {'exclude': False, 'ids': [7]},
            'target': {'refId': 'Anno', 'datasource': DS,
                       'query': 'import "array"\narray.from(rows: [\n  ' + ',\n  '.join(rows) +
                                '\n])\n  |> filter(fn: (r) => r.time >= v.timeRangeStart and r.time <= v.timeRangeStop)'}}


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
                   'Всплески ВО привязаны к восстановлению TPS: учтены также затупы малых шагов обоих тестов. '
                   'После всплеска — две затухающие волны за 40–60 с. Базовая волна около 6 минут, амплитуда около 4 мс; '
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
    dashboard['annotations']['list'] = [a for a in dashboard['annotations']['list'] if a.get('name') != 'Восстановление TPS'] + [recovery_annotation(events)]
    dashboard['version'] = 3
    for run in runs:
        start, *_, end = run['boundaries']
        link = {'type': 'link', 'title': run['name'] + ' · ВО',
                'url': f'/d/{UID}?from={(start - 120) * 1000}&to={(end + 120) * 1000}&timezone=Europe%2FMoscow&viewPanel=panel-7', 'targetBlank': False}
        dashboard['links'] = [x for x in dashboard.get('links', []) if x.get('title') != link['title']] + [link]
    save(dashboard_path, dashboard)
    metadata = {'revision': 1, 'measurement': MEASUREMENT, 'field': 'value', 'tag': 'trace', 'traces': list(BASES),
                'units': 'ms', 'base_ms': BASES, 'colors': COLORS, 'interval_seconds': 5,
                'points_per_trace': len(timestamps), 'points': len(lp), 'is_raw_telemetry': False,
                'description': description, 'runs': runs, 'recovery_events': events,
                'cycles': cycles, 'period_change_probability': .12, 'period_max_relative_change': .06,
                'amplitude_change_probability': .18, 'amplitude_max_relative_change': .12,
                'tps_csv_sha256': hashlib.sha256((ROOT / 'metrics.csv').read_bytes()).hexdigest(),
                'csv_sha256': hashlib.sha256((ROOT / 'response-time.csv').read_bytes()).hexdigest(),
                'reference_sha256': hashlib.sha256((ROOT / 'source/response-time-reference.png').read_bytes()).hexdigest(),
                'seed': 20261017}
    save(ROOT / 'response-time-metadata.json', metadata)
    tps_metadata['response_time'] = {k: v for k, v in metadata.items() if k not in {'cycles', 'recovery_events'}}
    save(ROOT / 'metadata.json', tps_metadata)
    render(timestamps, values, runs, events)
    print(json.dumps({'response_points': len(lp), 'recovery_events': len(events),
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
            axis.text(x1 + (x2 - x1) / 2, 562, label, ha='center', fontsize=10)
        for event in events:
            if event['run'] == run['name']:
                axis.axvspan(datetime.fromtimestamp(event['timestamp'], MSK), datetime.fromtimestamp(event['timestamp'] + 30, MSK), color='#B4326C', alpha=.2)
        axis.set(xlim=(datetime.fromtimestamp(start - 120, MSK), datetime.fromtimestamp(end + 120, MSK)), ylim=(0, 600), ylabel='ВО, мс')
        axis.set_title(run['name'] + ' · 17–18.10.2026, Москва', loc='right', fontsize=12)
        axis.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=MSK))
        axis.grid(alpha=.25)
        axis.legend(loc='lower left', bbox_to_anchor=(0, 1.01), ncol=4, fontsize=10)
    fig.savefig(ROOT / 'response-time-preview.png', dpi=125)
    plt.close(fig)


if __name__ == '__main__':
    build()
