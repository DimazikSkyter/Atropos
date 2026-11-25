"""Digitize the supplied images while retaining excursion widths and slopes.

The detailed light-background image is authoritative for the first orange high
plateau. The full original image supplies the remaining history and colors.
No test-fixture values are reused. Requires numpy, Pillow and matplotlib.
"""
import argparse
import csv
import hashlib
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD_ROOT = ROOT if (ROOT / 'build_overview.py').is_file() else ROOT.parent
os.environ.setdefault('MPLCONFIGDIR', str(ROOT / '.mplconfig'))
import numpy as np
from PIL import Image
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

MSK = timezone(timedelta(hours=3))
UID = 'screenshot-steps-20261017'
MEASUREMENT = 'screenshot_step_replay'
COLORS = {'orange': '#FF9830', 'green': '#73BF69', 'yellow': '#F2CC0C', 'blue': '#8AB8FF'}
NAMES = {'orange': 'Оранжевый', 'green': 'Зелёный', 'yellow': 'Жёлтый', 'blue': 'Голубой'}
DS = {'type': 'influxdb', 'uid': 'influxdb-replay'}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def centerline(mask):
    middle = np.full(mask.shape[1], np.nan)
    for x in range(mask.shape[1]):
        ys = np.flatnonzero(mask[:, x])
        if len(ys):
            middle[x] = np.median(ys)
    return middle


def sample_segments(path, point_x, segments):
    """Interpolate the visible stroke, retaining all intermediate pixel values."""
    result = np.zeros(len(point_x))
    for a, z in segments:
        good = np.flatnonzero(np.isfinite(path[a + 3:z - 3])) + a + 3
        assert len(good) > (z-a) * .25
        start, stop = [int(np.argmin(abs(point_x-boundary))) for boundary in (a, z)]
        result[start:stop] = (1070 - np.interp(point_x[start:stop], good, path[good])) * .2
    return result


def make_panel(pid, title, traces, x, y, w, h, description):
    targets, overrides = [], []
    for ref, trace in zip('ABCD', traces):
        targets.append({'refId': ref, 'datasource': DS, 'query':
            'import "date"\n\nfrom(bucket: v.defaultBucket)\n'
            '  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))\n'
            f'  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "value" and r.trace == "{trace}")\n'
            '  |> keep(columns: ["_time", "_value"])\n  |> group(columns: [])\n  |> sort(columns: ["_time"])'})
        overrides.append({'matcher': {'id': 'byFrameRefID', 'options': ref}, 'properties': [
            {'id': 'displayName', 'value': NAMES[trace]},
            {'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': COLORS[trace]}}]})
    defaults = {'unit': 'suffix:TPS', 'decimals': 2, 'custom': {
        'drawStyle': 'line', 'lineInterpolation': 'linear', 'lineWidth': 2,
        'fillOpacity': 5 if len(traces) > 1 else 0, 'showPoints': 'never',
        'spanNulls': False, 'axisPlacement': 'left', 'axisLabel': 'TPS',
        'scaleDistribution': {'type': 'linear'}, 'stacking': {'mode': 'none', 'group': 'A'}}}
    if len(traces) > 1:
        defaults.update({'min': 0, 'max': 190})
    return {'id': pid, 'type': 'timeseries', 'title': title, 'description': description,
        'datasource': DS, 'gridPos': {'x': x, 'y': y, 'w': w, 'h': h},
        'interval': '15s', 'maxDataPoints': 20000, 'targets': targets,
        'fieldConfig': {'defaults': defaults, 'overrides': overrides},
        'options': {'legend': {'showLegend': True, 'displayMode': 'list', 'placement': 'bottom', 'calcs': []},
            'tooltip': {'mode': 'multi', 'sort': 'desc'}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=ROOT / 'source')
    args = parser.parse_args()
    src = args.source_root
    original = src / 'screenshot-step-replay-reference.png'
    detail = src / 'orange-high-plateau-reference.png'
    rgb = np.asarray(Image.open(original).convert('RGB'), dtype=float)
    zoom = np.asarray(Image.open(detail).convert('RGB'), dtype=float)
    assert rgb.shape == (1186, 2495, 3) and zoom.shape == (956, 1010, 3)
    tick_x = np.array([243, 546, 849, 1152, 1454, 1757, 2059, 2362], dtype=float)
    pps, origin_x = np.polyfit(np.arange(len(tick_x)) * 7200, tick_x, 1)
    start = datetime(2026, 10, 17, 14, 23, 30, tzinfo=MSK)
    stamps = [start + timedelta(seconds=15*i) for i in range(3956)]
    origin_time = datetime(2026, 10, 17, 16, tzinfo=MSK)
    point_x = np.array([origin_x + (t-origin_time).total_seconds()*pps for t in stamps])
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    masks = {'orange': (r > 180) & (g > 90) & (g < 170) & (b < 110) & (r > 1.45*g) & (r < 2.4*g),
        'green': (g > 130) & (g > r*1.05) & (g > b*1.25) & (r > 65),
        'yellow': (r > 180) & (g > 160) & (b < 130) & (r/np.maximum(g, 1) < 1.5),
        'blue': (b > 135) & (b > r*1.10) & (g > 110) & (g > r*1.02)}
    bands = {'orange': (100, 1081), 'green': (400, 560), 'yellow': (865, 935), 'blue': (960, 1010)}
    paths = {}
    for trace, mask in masks.items():
        ya, yz = bands[trace]
        mask[:ya] = False
        mask[yz:] = False
        paths[trace] = centerline(mask)
    # The median across a near-vertical stroke can stop halfway to its visible
    # tip. Preserve isolated turns in the full screenshot without removing any
    # neighboring samples or shrinking their width.
    full_turns = {}
    for trace, path in paths.items():
        accepted = []
        segments = [(575, 893), (893, 1245), (1440, 1744), (1744, 2095)] if trace == 'orange' else [(893, 1245), (1744, 2095)]
        for a, z in segments:
            xcoords = np.arange(a+8, z-8)
            valid = np.flatnonzero(np.isfinite(path[a+8:z-8]))+a+8
            continuous = np.interp(xcoords, valid, path[valid])
            padded = np.pad(continuous, (15,15), mode='edge')
            background = np.median(np.lib.stride_tricks.sliding_window_view(padded,31), axis=-1)
            for sign in [1,-1]:
                detected = sign*(background-continuous) > (5 if trace in ['orange','green'] else 2)
                splits = np.flatnonzero(np.diff(np.r_[False,detected,False]))
                for begin, end in zip(splits[::2],splits[1::2]):
                    candidates = []
                    for xx in xcoords[max(0,begin-1):min(len(xcoords),end+1)]:
                        ys = np.flatnonzero(masks[trace][:,xx])
                        if len(ys):
                            candidates.append((int(ys.min()) if sign > 0 else int(ys.max()),int(xx)))
                    tip, xx = min(candidates) if sign > 0 else max(candidates)
                    path[xx] = tip
                    accepted.append({'source_x':xx,'source_y':float(tip),'sign':sign})
        full_turns[trace] = accepted
    high = [(893, 1245), (1744, 2095)]
    values = {'orange': sample_segments(paths['orange'], point_x, [(575, 893), *high, (1440, 1744)])}
    low = {'green': [(575, 689), (1440, 1540)], 'yellow': [(689, 791), (1540, 1642)],
        'blue': [(791, 893), (1642, 1744)]}
    for trace in ['green', 'yellow', 'blue']:
        values[trace] = sample_segments(paths[trace], point_x, high)
        for a, z in low[trace]:
            first, last = [int(np.argmin(abs(point_x-boundary))) for boundary in (a, z)]
            # Only these occluded lower sections inherit the visible orange
            # path; high-plateau slopes come from each color's own image pixels.
            values[trace][first:last] = values['orange'][first:last]
    # Keep the visible turn at a 15-second timestamp as well as its shoulders.
    for trace, turns in full_turns.items():
        for turn in turns:
            i = int(np.argmin(abs(point_x-turn['source_x'])))
            values[trace][i] = (1070-turn['source_y'])*.2
    r, g, b = zoom[:, :, 0], zoom[:, :, 1], zoom[:, :, 2]
    mask = (r > 120) & (r > 1.5*g) & (g > 35) & (g < 160) & (b < 100)
    mask[:170] = False
    mask[850:] = False
    white_path = centerline(mask)
    # Centerline medians shorten very sharp turns because one image column
    # contains both sides of the stroke. Retain the visible turn's tip at that
    # column while keeping its original shoulders; never erase its neighbors.
    turns = []
    for candidate, sign in [(189, 1), (234, -1), (264, 1), (311, -1), (332, 1),
            (385, -1), (409, 1), (462, -1), (486, 1), (537, -1), (560, 1),
            (614, -1), (634, 1), (679, -1), (712, 1)]:
        candidates = []
        for x in range(candidate-3, candidate+4):
            ys = np.flatnonzero(mask[:, x])
            if len(ys):
                candidates.append((int(ys.min()) if sign > 0 else int(ys.max()), x))
        y, x = min(candidates) if sign > 0 else max(candidates)
        # Two pixels compensate for half of the four-pixel drawing stroke.
        white_path[x] = y + 2 if sign > 0 else y - 2
        turns.append({'source_x': x, 'source_y': float(white_path[x]), 'sign': sign})
    detail_pps, detail_origin = np.polyfit(np.array([0, 1800, 3600, 5400, 7200]), np.array([176, 368, 559, 751, 942]), 1)
    detail_time = datetime(2026, 10, 17, 20, 30, tzinfo=MSK)
    detail_point_x = np.array([detail_origin + (t-detail_time).total_seconds()*detail_pps for t in stamps])
    good = np.flatnonzero(np.isfinite(white_path[110:973])) + 110
    in_detail = (detail_point_x >= 110) & (detail_point_x <= 972)
    # The detail has no numerical Y labels. Match its quiet plateau and highest
    # tip to the original screenshot's relative scale, preserving the shape.
    detail_scale = 12.0 / (500.0-285.5)
    detail_values = 166.8 + (500 - np.interp(detail_point_x[in_detail], good, white_path[good])) * detail_scale
    values['orange'][in_detail] = detail_values
    for turn in turns:
        nearest = int(np.argmin(abs(detail_point_x-turn['source_x'])))
        neighborhood = np.arange(nearest-2,nearest+3)
        i = int(neighborhood[np.argmax(values['orange'][neighborhood]) if turn['sign'] > 0 else np.argmin(values['orange'][neighborhood])])
        tip_value = 166.8 + (500-turn['source_y'])*detail_scale
        values['orange'][i] = max(values['orange'][i],tip_value) if turn['sign'] > 0 else min(values['orange'][i],tip_value)
        turn.update({'index': i, 'timestamp_msk': stamps[i].isoformat(), 'value': float(values['orange'][i])})
    with (ROOT / 'metrics.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp', 'trace', 'value'])
        for i, stamp in enumerate(stamps):
            for trace in COLORS:
                writer.writerow([stamp.isoformat(), trace, repr(float(values[trace][i]))])
    rows = list(csv.DictReader((ROOT / 'metrics.csv').open(encoding='utf-8')))
    lp = [f'{MEASUREMENT},trace={row["trace"]} value={row["value"]} {int(datetime.fromisoformat(row["timestamp"]).timestamp())}' for row in rows]
    (ROOT / 'metrics.lp').write_bytes(('\n'.join(lp)+'\n').encode())
    description = ('Реконструкция по изображениям, 17.10.2026 14:23:30 — 18.10.2026 06:52:15, Москва, шаг 15 секунд. '
        'Первая верхняя оранжевая ступень восстановлена по подробной картинке пользователя: '
        'сохранены видимая ширина, наклонные фронты и откаты ниже плато. Остальные участки и цвета '
        'прочитаны с полного скриншота. Единица TPS задана пользователем; масштаб восстановлен по картинке: нижнее оранжевое плато = 100 TPS. '
        'Скрытые нижние перекрытия приближённые. Красная горизонталь и синяя рамка — оформление. '
        'В Grafana используется линейное соединение всех точек без усреднения.')
    zoom_from = int(datetime(2026,10,17,20,20,tzinfo=MSK).timestamp()*1000)
    zoom_to = int(datetime(2026,10,17,22,35,tzinfo=MSK).timestamp()*1000)
    zoom_url = f'/d/{UID}?from={zoom_from}&to={zoom_to}&timezone=Europe%2FMoscow&viewPanel=panel-2'
    dashboard = {'id': None, 'uid': UID, 'title': 'Ступенчатые ряды со скриншота — 17.10.2026',
        'schemaVersion': 41, 'version': 2, 'editable': True, 'timezone': 'Europe/Moscow', 'refresh': '',
        'graphTooltip': 1, 'tags': ['atropos', 'influxdb', 'replay', 'screenshot'], 'description': description,
        'annotations': {'list': []}, 'templating': {'list': []},
        'links': [{'type': 'link', 'title': 'Верхняя оранжевая ступень крупнее', 'url': zoom_url, 'targetBlank': False}],
        'time': {k: t.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') for k, t in [('from', stamps[0]), ('to', stamps[-1])]},
        'panels': [make_panel(1, 'Сценарий', list(reversed(COLORS)), 0, 0, 24, 16, description)]}
    for i, trace in enumerate(COLORS):
        dashboard['panels'].append(make_panel(i+2, NAMES[trace]+' ряд', [trace], i%2*12, 16+i//2*9, 12, 9, description))
    dashboard['panels'].append({'id': 6, 'type': 'text', 'title': 'Источник и шкала',
        'gridPos': {'x': 0, 'y': 34, 'w': 24, 'h': 5}, 'options': {'mode': 'markdown', 'content': description}})
    save(DASHBOARD_ROOT/'dashboards'/(UID+'.json'), dashboard)
    meta = {'revision': 2, 'measurement': MEASUREMENT, 'field': 'value', 'tag': 'trace', 'traces': list(COLORS),
        'colors': COLORS, 'bucket': 'metrics-replay', 'organization': 'atropos', 'timezone': 'Europe/Moscow',
        'start_msk': stamps[0].isoformat(), 'end_msk': stamps[-1].isoformat(), 'interval_seconds': 15,
        'points_per_trace': len(stamps), 'points': len(rows), 'units': 'TPS', 'lower_orange_plateau': 100,
        'is_raw_telemetry': False, 'dashboard_uid': UID, 'dashboard_url': 'http://localhost:3001/d/'+UID,
        'zoom_url': 'http://localhost:3001'+zoom_url, 'description': description,
        'sources': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (original, detail)},
        'csv_sha256': hashlib.sha256((ROOT/'metrics.csv').read_bytes()).hexdigest(),
        'full_image_turns': full_turns,
        'detail_calibration': {'pixels_per_second': float(detail_pps), 'pixel_at_20_30': float(detail_origin),
            'plateau_y': 500, 'plateau_value': 166.8, 'units_per_pixel': detail_scale,
            'stroke_half_width_pixels': 2, 'narrow_excursions_collapsed': False}, 'turns': turns}
    save(ROOT/'metadata.json', meta)
    import response_time
    response_time.build()
    spec = importlib.util.spec_from_file_location('overview', DASHBOARD_ROOT/'build_overview.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.build()
    fig, axes = plt.subplots(2, 1, figsize=(16, 11), layout='constrained')
    axes[0].imshow(rgb.astype('uint8'), aspect='auto')
    axes[0].axis('off')
    axes[0].set_title('Полный исходный скриншот')
    axes[1].set_facecolor('#181b20')
    for trace in reversed(COLORS):
        axes[1].plot(stamps, values[trace], color=COLORS[trace], lw=1, label=NAMES[trace])
    axes[1].set_xlim(stamps[0], stamps[-1])
    axes[1].set_ylim(-3, 190)
    axes[1].xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 2), tz=MSK))
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=MSK))
    axes[1].grid(alpha=.2)
    axes[1].legend()
    axes[1].set_title('17–18 октября, Москва · 15 с · восстановление по изображениям')
    axes[1].set_ylabel('TPS')
    fig.savefig(ROOT/'preview.png', dpi=120)
    plt.close(fig)
    fig, axes = plt.subplots(2, 1, figsize=(12, 12), layout='constrained')
    axes[0].imshow(zoom.astype('uint8'))
    axes[0].axis('off')
    axes[0].set_title('Картинка пользователя — источник формы')
    axes[1].plot(stamps, values['orange'], color='#B65C00', lw=2)
    axes[1].set_xlim(datetime(2026,10,17,20,20,tzinfo=MSK), datetime(2026,10,17,22,35,tzinfo=MSK))
    axes[1].set_ylim(148, 183)
    axes[1].xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0,30], tz=MSK))
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=MSK))
    axes[1].grid(alpha=.25)
    axes[1].set_title('Исправленный ряд: сохранены пики, откаты и их ширина')
    axes[1].set_ylabel('TPS')
    fig.savefig(ROOT/'upper-plateau-comparison.png', dpi=110)
    plt.close(fig)
    print(json.dumps({'points': len(rows), 'source': detail.name, 'turns': len(turns), 'zoom_url': meta['zoom_url']}))


if __name__ == '__main__':
    main()
