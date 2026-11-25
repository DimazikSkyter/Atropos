"""Digitize the supplied service RPS screenshot without adding synthetic noise.

Uses the Windows image decoder and the Python standard library only.
"""
import bisect
import csv
import hashlib
import json
import math
import statistics
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=3))
REFERENCE_TIME = datetime(2026, 10, 4, 20, 30, tzinfo=TZ)
PALETTE = {
    'service1': ('green', (115, 191, 105)),
    'service2': ('yellow', (242, 204, 12)),
    'service3': ('blue', (138, 184, 255)),
    'service4': ('orange', (255, 120, 10)),
    'service5': ('red', (242, 73, 92)),
}


def linear_fit(xs, ys):
    xm, ym = statistics.fmean(xs), statistics.fmean(ys)
    slope = sum((x-xm)*(y-ym) for x, y in zip(xs, ys))/sum((x-xm)**2 for x in xs)
    return slope, ym-slope*xm


def interpolate(xs, ys, x):
    i = bisect.bisect_right(xs, x)
    if i == 0:
        return ys[0]
    if i == len(xs):
        return ys[-1]
    return ys[i-1] + (ys[i]-ys[i-1])*(x-xs[i-1])/(xs[i]-xs[i-1])


def main():
    with tempfile.TemporaryDirectory(dir=ROOT) as temp:
        raw_path = Path(temp)/'pixels.bgra'
        info = json.loads(subprocess.check_output([
            'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT/'bitmap_tools.ps1'),
            '-Mode', 'extract', '-InputPath', str(ROOT/'reference.png'), '-OutputPath', str(raw_path),
        ], text=True))
        raw = raw_path.read_bytes()
    stride = info['stride']

    def rgb(x, y):
        i = y*stride+x*4
        b, g, r = raw[i:i+3]
        return r, g, b

    def grid_center(center, horizontal):
        coordinates = range(center-6, center+7)
        weights = [max(0, rgb(1000, v)[1]-27) if horizontal else max(0, rgb(v, 900)[1]-27)
                   for v in coordinates]
        return sum(x*w for x, w in zip(coordinates, weights))/sum(weights)

    x_grid = [grid_center(x, False) for x in [470, 818, 1162, 1509, 1857, 2205]]
    seconds_per_pixel, x_intercept = linear_fit(x_grid, [i*1800 for i in range(6)])
    y_grid = [grid_center(y, True) for y in [997, 768, 538, 309, 80]]
    rps_per_pixel, y_intercept = linear_fit(y_grid, [0, 500, 1000, 1500, 2000])
    traces = {s: {} for s in PALETTE}
    for x in range(434, info['width']):
        candidates = {s: [] for s in PALETTE}
        for y in range(100, 515):
            r, g, b = rgb(x, y)
            if max(r, g, b) < 90:
                continue
            distances = {s: (r-pr)**2+(g-pg)**2+(b-pb)**2 for s, (_, (pr, pg, pb)) in PALETTE.items()}
            service = min(distances, key=distances.get)
            distance = distances[service]
            if distance < 3600:
                candidates[service].append((y, distance))
        for service, points in candidates.items():
            # The cropped left edge has two orange-looking pixels produced by
            # the yellow/red overlap, well below the actual orange stroke.
            if not points or (service == 'service1' and x < 2091) or (service == 'service4' and x < 438):
                continue
            best = min(d for _, d in points)
            core = [y for y, d in points if d <= best+50]
            traces[service][x] = statistics.median(core)

    rows, trace_rows, services = [], [], {}
    for service, trace in traces.items():
        xs = sorted(trace)
        ys = [trace[x] for x in xs]
        start_second = math.ceil((xs[0]*seconds_per_pixel+x_intercept)/15)*15
        end_second = math.floor((xs[-1]*seconds_per_pixel+x_intercept)/15)*15
        points = []
        for second in range(start_second, end_second+1, 15):
            x = (second-x_intercept)/seconds_per_pixel
            y = interpolate(xs, ys, x)
            value = round(y*rps_per_pixel+y_intercept, 6)
            stamp = REFERENCE_TIME+timedelta(seconds=second)
            points.append((stamp, value))
            rows.append((stamp.isoformat(), service, f'{value:.6f}', int(stamp.timestamp())))
        trace_rows.extend((service, x, f'{y:.3f}', f'{y*rps_per_pixel+y_intercept:.6f}') for x, y in trace.items())
        gaps = [(a+1, b-1) for a, b in zip(xs, xs[1:]) if b-a > 1]
        services[service] = {
            'color': PALETTE[service][0], 'hex': '#%02X%02X%02X' % PALETTE[service][1],
            'points': len(points), 'first': points[0][0].isoformat(), 'last': points[-1][0].isoformat(),
            'min_rps': min(v for _, v in points), 'max_rps': max(v for _, v in points),
            'observed_columns': len(xs), 'occluded_column_ranges': gaps,
        }
    rows.sort(key=lambda row: (row[3], row[1]))
    with (ROOT/'metrics.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f); writer.writerow(['timestamp', 'service', 'rps']); writer.writerows(r[:3] for r in rows)
    (ROOT/'metrics.lp').write_bytes(''.join(f'service_rps,service={s} value={v} {epoch}\n' for _, s, v, epoch in rows).encode('utf-8'))
    with (ROOT/'reference-traces.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f); writer.writerow(['service', 'source_x', 'source_y', 'rps']); writer.writerows(trace_rows)
    metadata = {
        'measurement': 'service_rps', 'field': 'value', 'tag': 'service', 'unit': 'requests/s',
        'bucket': 'metrics-replay', 'timezone': 'Europe/Moscow', 'date': '2026-10-04',
        'interval_seconds': 15, 'points': len(rows),
        'start': min(r[0] for r in rows), 'end': max(r[0] for r in rows),
        'dashboard_uid': 'service-rps-20261004', 'services': services,
        'source_sha256': hashlib.sha256((ROOT/'reference.png').read_bytes()).hexdigest(),
        'calibration': {'x_grid_centers': x_grid, 'x_grid_times': ['20:30', '21:00', '21:30', '22:00', '22:30', '23:00'],
                        'seconds_per_pixel': seconds_per_pixel, 'x_intercept_seconds_from_2030': x_intercept,
                        'y_grid_centers': y_grid, 'y_grid_rps': [0, 500, 1000, 1500, 2000],
                        'rps_per_pixel': rps_per_pixel, 'y_intercept_rps': y_intercept},
        'provenance': 'Approximate digitization of the user-supplied screenshot. Colors assign service identities; original names are unavailable.',
        'method': 'Nearest palette color; median of the strongest stroke pixels in each column; linear interpolation to a 15-second grid. No random noise.',
        'limitations': 'Moscow timezone assumed. Raster precision is about 5.2 seconds per x pixel and 2.18 RPS per y pixel. Overlapping strokes are interpolated only between visible same-color anchors. Green service1 begins around 22:50; its invisible earlier history is not fabricated. Original sampling cadence is unknown.',
    }
    (ROOT/'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    build_dashboard(metadata)
    subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT/'bitmap_tools.ps1'),
                    '-Mode', 'preview', '-InputPath', str(ROOT/'metrics.csv'), '-OutputPath', str(ROOT/'preview.png')], check=True)
    print(json.dumps({'points': len(rows), 'services': services, 'calibration': metadata['calibration']}, indent=2))


def build_dashboard(meta):
    datasource = {'type': 'influxdb', 'uid': 'influxdb-replay'}
    targets, overrides = [], []
    for ref, (service, (_, color)) in zip('ABCDE', PALETTE.items()):
        query = ('import "date"\n\nfrom(bucket: v.defaultBucket)\n'
                 '  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))\n'
                 f'  |> filter(fn: (r) => r._measurement == "service_rps" and r._field == "value" and r.service == "{service}")\n'
                 '  |> keep(columns: ["_time", "_value"])\n  |> group(columns: [])\n  |> sort(columns: ["_time"])')
        targets.append({'refId': ref, 'datasource': datasource, 'query': query})
        overrides.append({'matcher': {'id': 'byFrameRefID', 'options': ref}, 'properties': [
            {'id': 'displayName', 'value': service},
            {'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': '#%02X%02X%02X' % color}},
        ]})
    targets.append({'refId': 'F', 'datasource': datasource, 'query': (
        'import "date"\n\nfrom(bucket: v.defaultBucket)\n'
        '  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))\n'
        '  |> filter(fn: (r) => r._measurement == "service_rps" and r._field == "value")\n'
        '  |> filter(fn: (r) => r.service =~ /^service[1-5]$/)\n'
        '  |> keep(columns: ["_time", "_value"])\n'
        '  |> group(columns: ["_time"])\n'
        '  |> sum(column: "_value")\n'
        '  |> group(columns: [])\n'
        '  |> sort(columns: ["_time"])')})
    overrides.append({'matcher': {'id': 'byFrameRefID', 'options': 'F'}, 'properties': [
        {'id': 'displayName', 'value': 'Сумма RPS'},
        {'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': '#B877D9'}},
        {'id': 'custom.lineWidth', 'value': 3},
        {'id': 'custom.axisPlacement', 'value': 'right'},
        {'id': 'custom.axisLabel', 'value': 'Суммарный RPS'},
        {'id': 'min', 'value': 0}, {'id': 'max', 'value': 10000},
    ]})
    description = ('Приблизительное восстановление RPS по предоставленному изображению. '
                   '4 октября 2026 года, 20:27–23:27, Москва. Шаг 15 секунд, без добавления случайного шума. '
                   'service1 — зелёный, service2 — жёлтый, service3 — голубой, service4 — оранжевый, service5 — красный. '
                   'Зелёная линия видна только примерно с 22:50; более ранние значения service1 неизвестны. '
                   'Фиолетовая кривая «Сумма RPS» (правая шкала) складывает доступные значения сервисов '
                   'в каждой временной точке без усреднения и заполнения пропусков. До 22:50 service1 в сумму не входит.')
    dashboard = {
        'id': None, 'uid': meta['dashboard_uid'], 'title': 'RPS сервисов — 04.10.2026',
        'schemaVersion': 41, 'version': 1, 'editable': True, 'timezone': 'Europe/Moscow',
        'refresh': '', 'graphTooltip': 1, 'tags': ['atropos', 'influxdb', 'replay', 'rps'],
        'description': description, 'annotations': {'list': []}, 'templating': {'list': []},
        'time': {'from': '2026-10-04T17:27:00Z', 'to': '2026-10-04T20:27:00Z'},
        'panels': [{'id': 1, 'type': 'timeseries', 'title': 'RPS сервисов', 'description': description,
                    'datasource': datasource, 'gridPos': {'x': 0, 'y': 0, 'w': 24, 'h': 20},
                    'interval': '15s', 'maxDataPoints': 10000, 'targets': targets,
                    'fieldConfig': {'defaults': {'unit': 'reqps', 'min': 0, 'max': 2200, 'decimals': 0,
                        'custom': {'drawStyle': 'line', 'lineInterpolation': 'linear', 'lineWidth': 2,
                                   'fillOpacity': 0, 'showPoints': 'never', 'spanNulls': False,
                                   'axisPlacement': 'left', 'axisLabel': 'RPS',
                                   'scaleDistribution': {'type': 'linear'}, 'stacking': {'mode': 'none', 'group': 'A'}}},
                                    'overrides': overrides},
                    'options': {'legend': {'showLegend': True, 'displayMode': 'list', 'placement': 'bottom', 'calcs': []},
                                'tooltip': {'mode': 'multi', 'sort': 'none'}}}],
    }
    destination = ROOT.parent/'dashboards'
    destination.mkdir(exist_ok=True)
    (destination/'service-rps-20261004.json').write_text(json.dumps(dashboard, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
