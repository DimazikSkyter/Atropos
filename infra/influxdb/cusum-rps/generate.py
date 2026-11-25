"""Reconstruct the green screenshot trace as a dated, 15-second RPS replay."""
import argparse
import bisect
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path


def interpolate(xs, ys, x):
    i = max(0, min(bisect.bisect_right(xs, x) - 1, len(xs) - 2))
    ratio = (x - xs[i]) / (xs[i + 1] - xs[i])
    return ys[i] + ratio * (ys[i + 1] - ys[i])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path,
                        default=Path(__file__).resolve().parents[3] / 'detectors/tests/fixtures')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    calibration = json.loads((args.fixtures / 'real-system-replay.json').read_text(encoding='utf-8'))
    with (args.fixtures / 'real-system-pixels.csv').open(encoding='utf-8', newline='') as source:
        pixels = list(csv.DictReader(source))
    xs = [int(row['pixel_x']) for row in pixels]
    ys = [float(row['envelope_pixel_y']) for row in pixels]
    assumptions = calibration['assumptions']
    x_start = assumptions['time_tick_13_00_x']
    pixels_per_second = (assumptions['time_tick_next_day_10_00_x'] - x_start) / (21 * 3600)
    start = datetime(2026, 9, 22, 13, tzinfo=timezone(timedelta(hours=3)))
    duration = (xs[-1] - x_start) / pixels_per_second
    samples = []
    for offset in range(0, int(duration) + 1, 15):
        x = x_start + offset * pixels_per_second
        value = 1000 + (assumptions['envelope_baseline_pixel_y'] - interpolate(xs, ys, x)) * assumptions['units_per_vertical_pixel']
        samples.append((start + timedelta(seconds=offset), round(value, 6)))
    with (args.output / 'rps.csv').open('w', encoding='utf-8', newline='') as target:
        writer = csv.writer(target)
        writer.writerow(['timestamp', 'rps'])
        writer.writerows((timestamp.isoformat(), value) for timestamp, value in samples)
    # A dedicated tag makes re-imports idempotent and isolates this reconstruction.
    (args.output / 'rps.lp').write_text(''.join(
        f'rps,replay=cusum-real-system-image value={value:.6f} {int(timestamp.timestamp())}\n'
        for timestamp, value in samples), encoding='utf-8', newline='\n')
    end = samples[-1][0]
    utc_iso = lambda value: value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    query = ('import "date"\n\nfrom(bucket: v.defaultBucket)\n'
             '  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))\n'
             '  |> filter(fn: (r) => r._measurement == "rps" and r._field == "value")\n'
             '  |> filter(fn: (r) => r.replay == "cusum-real-system-image")\n'
             '  |> keep(columns: ["_time", "_value"])\n'
             '  |> group(columns: [])\n  |> sort(columns: ["_time"])')
    datasource = {'type': 'influxdb', 'uid': 'influxdb-replay'}
    dashboard = {
        'id': None, 'uid': 'cusum-rps-20260922', 'version': 1, 'schemaVersion': 41,
        'title': 'CUSUM — RPS, 22–23 сентября 2026', 'editable': True,
        'timezone': 'Europe/Moscow', 'refresh': '', 'graphTooltip': 1,
        'tags': ['atropos', 'replay', 'cusum', 'rps'],
        'description': 'Приближённая реконструкция зелёной линии из изображения. Основной уровень 1000 RPS; условная вертикальная шкала 1 RPS/пиксель. Сетка 15 с, линейная интерполяция по пикселям. Это не исходная телеметрия.',
        'time': {'from': utc_iso(start), 'to': utc_iso(end)},
        'annotations': {'list': []}, 'links': [], 'templating': {'list': []},
        'panels': [{
            'id': 1, 'type': 'timeseries', 'title': 'RPS · шаг 15 секунд · МСК',
            'description': 'Зелёная линия с исходного изображения: уровень 1000, условный масштаб 1 RPS/пиксель. Без агрегации; точки интерполированы с шагом 15 секунд.',
            'datasource': datasource, 'gridPos': {'h': 19, 'w': 24, 'x': 0, 'y': 0},
            'maxDataPoints': 10000, 'interval': '15s',
            'targets': [{'refId': 'A', 'datasource': datasource, 'query': query}],
            'fieldConfig': {'defaults': {
                'displayName': 'rps', 'unit': 'reqps', 'min': 0, 'max': 1100, 'decimals': 1,
                'color': {'mode': 'fixed', 'fixedColor': '#73BF69'},
                'custom': {'drawStyle': 'line', 'lineInterpolation': 'linear', 'lineWidth': 1,
                           'fillOpacity': 3, 'showPoints': 'never', 'spanNulls': False,
                           'axisPlacement': 'left', 'axisLabel': 'RPS',
                           'scaleDistribution': {'type': 'linear'},
                           'stacking': {'mode': 'none', 'group': 'A'}}}, 'overrides': []},
            'options': {'legend': {'showLegend': True, 'displayMode': 'list', 'placement': 'bottom', 'calcs': []},
                        'tooltip': {'mode': 'single', 'sort': 'none', 'hideZeros': False}},
        }],
    }
    (args.output / 'cusum-rps-20260922.json').write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    manifest = {
        'scenario': 'real-system-grafana-15s', 'measurement': 'rps', 'field': 'value',
        'tags': {'replay': 'cusum-real-system-image'}, 'bucket': 'metrics-replay', 'organization': 'atropos',
        'points': len(samples), 'sample_step_seconds': 15, 'start': start.isoformat(), 'end': end.isoformat(),
        'timezone': 'Europe/Moscow', 'baseline_rps': 1000, 'units_per_vertical_pixel': assumptions['units_per_vertical_pixel'],
        'min_rps': min(v for _, v in samples), 'max_rps': max(v for _, v in samples),
        'is_raw_telemetry': False, 'method': '98th-percentile green lower envelope; linear interpolation of per-pixel trace',
        'source_sha256': calibration['source_sha256'],
        'pixels_sha256': hashlib.sha256((args.fixtures / 'real-system-pixels.csv').read_bytes()).hexdigest(),
        'csv_sha256': hashlib.sha256((args.output / 'rps.csv').read_bytes()).hexdigest(),
        'dashboard_uid': dashboard['uid'],
        'dashboard_url': f'http://localhost:3001/d/{dashboard["uid"]}?orgId=1&from={int(start.timestamp()*1000)}&to={int(end.timestamp()*1000)}&timezone=Europe%2FMoscow',
        'screenshot': 'real-system-grafana.jpg',
    }
    (args.output / 'metadata.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
