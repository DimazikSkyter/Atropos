"""Reconstruct four service response-time traces from the supplied screenshot."""
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
START = datetime(2026, 10, 5, tzinfo=timezone(timedelta(hours=3)))
END_SECOND = 11*3600+50*60
PALETTE = {'service1': ('green', '#73BF69'), 'service2': ('yellow', '#F2CC0C'),
           'service3': ('orange', '#FF9830'), 'service4': ('red', '#F2495C')}


def fit(xs, ys):
    xm, ym = statistics.fmean(xs), statistics.fmean(ys)
    slope = sum((x-xm)*(y-ym) for x, y in zip(xs, ys))/sum((x-xm)**2 for x in xs)
    return slope, ym-slope*xm


def interpolate(xs, ys, x):
    i = bisect.bisect_right(xs, x)
    if i == 0: return ys[0]
    if i == len(xs): return ys[-1]
    return ys[i-1]+(ys[i]-ys[i-1])*(x-xs[i-1])/(xs[i]-xs[i-1])


def extract():
    with tempfile.TemporaryDirectory(dir=ROOT) as temp:
        raw_path = Path(temp)/'pixels.bgra'
        info = json.loads(subprocess.check_output([
            'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT/'bitmap_tools.ps1'),
            '-Mode', 'extract', '-InputPath', str(ROOT/'reference.png'), '-OutputPath', str(raw_path)], text=True))
        raw = raw_path.read_bytes()
    stride = info['stride']
    columns = {c: {} for c, _ in PALETTE.values()}
    for x in range(30, 2085):
        candidates = {c: [] for c in columns}
        for y in range(75, 1215):
            i = y*stride+x*4; b, g, r = raw[i:i+3]
            if r > 85 and g > 75 and .78 < g/r < 1.4 and b < min(r, g)*.8:
                color, strength = 'yellow', min(r, g)-b
            elif r > 95 and .3 < g/r < .70 and b < g*.85:
                color, strength = 'orange', r-max(g, b)
            elif g > 75 and g > r*1.25 and g > b*1.12:
                color, strength = 'green', g-max(r, b)
            elif r > 90 and g < r*.68 and g*1.02 < b < r*.9:
                color, strength = 'red', r-max(g, b)
            else: continue
            candidates[color].append((y, strength))
        for color, pts in candidates.items():
            if not pts: continue
            # Keep connected stroke candidates; select a continuous path
            # below so compression fringes cannot jump to another curve.
            runs = [[]]
            for item in pts:
                if runs[-1] and item[0]-runs[-1][-1][0] > 3: runs.append([])
                runs[-1].append(item)
            options = []
            for run in runs:
                peak = max(s for _, s in run)
                core = [y for y, s in run if s >= peak*.9]
                envelope = [y for y, s in run if s >= peak*.35]
                options.append({'y': statistics.median(core), 'upper': min(envelope), 'lower': max(envelope),
                                'strength': peak, 'height': len(run)})
            columns[color][x] = options
    traces = {}
    initial_rows = {'green': 835, 'yellow': 410, 'orange': 650, 'red': 835}
    for color, column in columns.items():
        xs = sorted(column); costs, parents = [], []
        for j, x in enumerate(xs):
            current_costs, current_parents = [], []
            for option in column[x]:
                emission = -option['strength']/60+3/max(option['height'], 1)
                if j == 0:
                    current_costs.append(emission+abs(option['y']-initial_rows[color])/12)
                    current_parents.append(None)
                else:
                    transitions = [costs[-1][k]+abs(option['y']-prior['y'])/(12*math.sqrt(x-xs[j-1]))
                                   for k, prior in enumerate(column[xs[j-1]])]
                    best = min(range(len(transitions)), key=transitions.__getitem__)
                    current_costs.append(emission+transitions[best]); current_parents.append(best)
            costs.append(current_costs); parents.append(current_parents)
        state = min(range(len(costs[-1])), key=costs[-1].__getitem__); path = {}
        for j in range(len(xs)-1, -1, -1):
            path[xs[j]] = column[xs[j]][state].copy(); state = parents[j][state]
        traces[color] = dict(sorted(path.items()))
    # Preserve the sharp tips of tall narrow strokes. Median-only extraction
    # would cut them off, especially near the end of the supplied image.
    tip_counts = {}
    for color, trace in traces.items():
        xs = sorted(trace); candidates = []
        for j, x in enumerate(xs):
            neighbors = [trace[z] for z in xs[max(0, j-5):j+6] if abs(z-x) <= 6]
            middle = statistics.median(v['y'] for v in neighbors)
            for key, sign in [('upper', -1), ('lower', 1)]:
                extreme = trace[x][key]
                if sign*(extreme-middle) > 20 and all(sign*extreme >= sign*v[key] for v in neighbors):
                    candidates.append((x, key, extreme, sign))
        merged = []
        for item in candidates:
            if merged and item[1] == merged[-1][1] and item[0]-merged[-1][0] <= 6:
                if item[3]*item[2] > merged[-1][3]*merged[-1][2]: merged[-1] = item
            else: merged.append(item)
        for x, _, extreme, sign in merged:
            trace[x]['y'] = extreme-sign*2
        tip_counts[color] = len(merged)
    centers = []
    for x0 in [42, 387, 732, 1077, 1422, 1767]:
        coords = range(x0-7, x0+8)
        weights = [max(raw[50*stride+x*4+1]-27, 0) for x in coords]
        centers.append(sum(x*w for x, w in zip(coords, weights))/sum(weights))
    seconds_per_pixel, time_intercept = fit(centers, [i*7200 for i in range(6)])
    baseline_rows = {color: statistics.median(v['y'] for x, v in trace.items() if 80 <= x <= 700)
                     for color, trace in traces.items()}
    lower_row = statistics.fmean([baseline_rows['green'], baseline_rows['red']])
    upper_row = baseline_rows['yellow']
    ms_per_pixel = (70-40)/(upper_row-lower_row)
    value_intercept = 40-ms_per_pixel*lower_row
    calibration = {'time_grid_centers_x': centers, 'time_grid_labels': ['00:00','02:00','04:00','06:00','08:00','10:00'],
                   'seconds_per_pixel': seconds_per_pixel, 'time_intercept': time_intercept,
                   'baseline_rows': baseline_rows, 'lower_baseline_ms': 40, 'upper_baseline_ms': 70,
                   'lower_baseline_y': lower_row, 'upper_baseline_y': upper_row,
                   'ms_per_pixel': ms_per_pixel, 'value_intercept_ms': value_intercept,
                   'preserved_sharp_tips': tip_counts}
    return traces, calibration


def build_dashboard(meta):
    ds = {'type': 'influxdb', 'uid': 'influxdb-replay'}
    description = ('Приблизительное восстановление четырёх рядов времени отклика по изображению. '
                   '5 октября 2026 года, 00:00–11:50, Europe/Moscow; шаг 15 секунд. '
                   'Единица мс подтверждена пользователем. Вертикальная шкала на изображении обрезана: '
                   'основные нижние уровни приняты за 40 мс, верхний — за 70 мс. '
                   'Зелёный — service1, жёлтый — service2, оранжевый — service3, красный — service4. '
                   'Голубая линия внизу — граница графика, не сервис. Имена условные, связи с ранее загруженным RPS не задаются.')
    targets, overrides = [], []
    for ref, (service, (_, color)) in zip('ABCD', PALETTE.items()):
        query = ('import "date"\n\nfrom(bucket: v.defaultBucket)\n'
                 '  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))\n'
                 f'  |> filter(fn: (r) => r._measurement == "service_response_time" and r._field == "value" and r.service == "{service}")\n'
                 '  |> keep(columns: ["_time", "_value"])\n  |> group(columns: [])\n  |> sort(columns: ["_time"])')
        targets.append({'refId': ref, 'datasource': ds, 'query': query})
        overrides.append({'matcher': {'id': 'byFrameRefID', 'options': ref}, 'properties': [
            {'id': 'displayName', 'value': service}, {'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': color}}]})
    dashboard = {'id': None, 'uid': meta['dashboard_uid'], 'title': 'Время отклика сервисов — 05.10.2026',
                 'schemaVersion': 41, 'version': 1, 'editable': True, 'timezone': 'Europe/Moscow',
                 'refresh': '', 'graphTooltip': 1, 'tags': ['atropos', 'influxdb', 'replay', 'latency'],
                 'description': description, 'annotations': {'list': []}, 'templating': {'list': []},
                 'time': {'from': '2026-10-04T21:00:00Z', 'to': '2026-10-05T08:50:00Z'},
                 'panels': [{'id': 1, 'type': 'timeseries', 'title': 'Время отклика сервисов', 'description': description,
                             'datasource': ds, 'gridPos': {'x': 0, 'y': 0, 'w': 24, 'h': 20},
                             'interval': '15s', 'maxDataPoints': 20000, 'targets': targets,
                             'fieldConfig': {'defaults': {'unit': 'ms', 'min': 0, 'max': 100, 'decimals': 2,
                                 'custom': {'drawStyle': 'line', 'lineInterpolation': 'linear', 'lineWidth': 2,
                                            'fillOpacity': 0, 'showPoints': 'never', 'spanNulls': False,
                                            'axisPlacement': 'left', 'axisLabel': 'мс',
                                            'scaleDistribution': {'type': 'linear'}, 'stacking': {'mode': 'none', 'group': 'A'}}},
                                             'overrides': overrides},
                             'options': {'legend': {'showLegend': True, 'displayMode': 'list', 'placement': 'bottom', 'calcs': []},
                                         'tooltip': {'mode': 'multi', 'sort': 'none'}}}]}
    directory = ROOT.parent/'dashboards'; directory.mkdir(exist_ok=True)
    (directory/'service-response-time-20261005.json').write_text(json.dumps(dashboard, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def main():
    traces, calibration = extract()
    rows, source_rows, services = [], [], {}
    for service, (color, hex_color) in PALETTE.items():
        trace = traces[color]; xs = sorted(trace); ys = [trace[x]['y'] for x in xs]
        values = []
        for second in range(0, END_SECOND+1, 15):
            x = (second-calibration['time_intercept'])/calibration['seconds_per_pixel']
            # Allow rounding the raster edge to the nearest half pixel.
            assert xs[0]-.5 <= x <= xs[-1]+.5, (service, second, x)
            y = interpolate(xs, ys, x)
            value = round(y*calibration['ms_per_pixel']+calibration['value_intercept_ms'], 6)
            stamp = START+timedelta(seconds=second)
            rows.append((stamp.isoformat(), service, f'{value:.6f}', int(stamp.timestamp())))
            values.append((second, value))
        early = [v for s, v in values if s < 4*3600]; late = [v for s, v in values if s >= 10*3600]
        services[service] = {'color': color, 'hex': hex_color, 'points': len(values),
                             'min_ms': min(v for _, v in values), 'max_ms': max(v for _, v in values),
                             'baseline_mean_ms': statistics.fmean(early), 'baseline_sigma_ms': statistics.pstdev(early),
                             'late_sigma_ms': statistics.pstdev(late), 'observed_columns': len(xs),
                             'interpolated_occluded_columns': sum(b-a-1 for a, b in zip(xs, xs[1:]))}
        source_rows.extend((service, x, f'{trace[x]["y"]:.3f}') for x in xs)
    rows.sort(key=lambda row: (row[3], row[1]))
    with (ROOT/'metrics.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f); writer.writerow(['timestamp', 'service', 'response_time_ms']); writer.writerows(r[:3] for r in rows)
    (ROOT/'metrics.lp').write_bytes(''.join(f'service_response_time,service={s} value={v} {epoch}\n' for _, s, v, epoch in rows).encode('utf-8'))
    with (ROOT/'reference-traces.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f); writer.writerow(['service', 'source_x', 'source_y']); writer.writerows(source_rows)
    meta = {'measurement': 'service_response_time', 'field': 'value', 'tag': 'service', 'unit': 'ms',
            'unit_confirmed_by_user': True, 'four_series_excluding_blue_border_confirmed_by_user': True,
            'date': '2026-10-05', 'timezone': 'Europe/Moscow', 'interval_seconds': 15,
            'start': START.isoformat(), 'end': (START+timedelta(seconds=END_SECOND)).isoformat(),
            'points': len(rows), 'points_per_service': END_SECOND//15+1, 'services': services,
            'dashboard_uid': 'service-response-time-20261005', 'calibration': calibration,
            'source_sha256': hashlib.sha256((ROOT/'reference.png').read_bytes()).hexdigest(),
            'method': 'Chromatic stroke extraction, connected-component rejection of JPEG fringes, sharp envelope tips, interpolation to a 15-second grid. No random noise.',
            'limitations': 'Approximate raster reconstruction. Vertical labels and legend are absent. Affine scale fits the user-provided approximate 40–70 ms baseline range; amplitudes are illustrative estimates. Short occlusions are interpolated. The Moscow timezone is assumed. Service names are independent of the previous RPS dataset. Cropped pixels before 00:00 are excluded to keep all timestamps on the requested local date.'}
    (ROOT/'metadata.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    build_dashboard(meta)
    subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT/'bitmap_tools.ps1'),
                    '-Mode', 'preview', '-InputPath', str(ROOT/'metrics.csv'), '-OutputPath', str(ROOT/'preview.png')], check=True)
    print(json.dumps({'points': len(rows), 'services': services, 'calibration': calibration}, indent=2))


if __name__ == '__main__': main()
