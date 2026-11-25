"""Approximate digitization of the two user screenshots; no invented noise."""
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import rgb_to_hsv
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'replay' if ROOT.name == 'correlation-change-staging' else ROOT
MSK = timezone(timedelta(hours=3))
SOURCES = {
    'latency': Path(r'C:\Users\dimaz\AppData\Local\Temp\codex-clipboard-587b5756-e5ce-46c7-bbc5-c2502e719cea.png'),
    'read_operations': Path(r'C:\Users\dimaz\AppData\Local\Temp\codex-clipboard-8d2194c8-62a0-4603-8b0e-83ff4eecadb8.png'),
}
COLORS = {'blue': '#5794f2', 'green': '#73bf69', 'yellow': '#f2cc0c',
          'orange': '#ff9830', 'red': '#f2495c', 'purple': '#b877d9'}


def extract(path, case):
    rgb = np.asarray(Image.open(path).convert('RGB'), dtype=float) / 255
    h, s, v = np.moveaxis(rgb_to_hsv(rgb), -1, 0)
    if case == 'latency':
        assert rgb.shape[:2] == (425, 2505)
        masks = {
            'blue': (h > .52) & (h < .66) & (s > .13) & (v > .28),
            'green': (h > .20) & (h < .47) & (s > .15) & (v > .23),
            'yellow': (h > .105) & (h < .20) & (s > .45) & (v > .28),
        }
        left, right, top, bottom = 108, 2455, 25, 365
        seconds_per_pixel, origin_x = 300 / 420, 310
        origin_time = datetime(2026, 10, 18, 16, 55, tzinfo=MSK)
        zero_y, value_per_pixel, step, unit = 370, 15 / (370-44), 15, 'ms'
    else:
        assert rgb.shape[:2] == (661, 1435)
        masks = {
            'blue': (h > .51) & (h < .67) & (s > .30) & (v > .21),
            'orange': (h > .045) & (h < .14) & (s > .3) & (v > .28),
            'red': ((h < .04) | (h > .96)) & (s > .30) & (v > .26),
            'purple': (h > .78) & (h < .91) & (s > .27) & (v > .24)
                      & (rgb[:, :, 1] < .72*np.minimum(rgb[:, :, 0], rgb[:, :, 2])),
        }
        left, right, top, bottom = 64, 1247, 35, 610
        # Half-hour tick labels: 19:00 at x=82, 23:30 at 405, 11:00 at 1233.
        seconds_per_pixel, origin_x = 1800 / 35.97, 82
        origin_time = datetime(2026, 10, 18, 19, 0, tzinfo=MSK)
        zero_y, value_per_pixel, step, unit = 684, 100 / 72.2, 60, 'ops/s'
    xs = np.arange(left, right+1)
    first = int(np.ceil((left-origin_x)*seconds_per_pixel/step))*step
    last = int(np.floor((right-origin_x)*seconds_per_pixel/step))*step
    seconds = np.arange(first, last+1, step)
    sample_x = origin_x + seconds / seconds_per_pixel
    traces, quality, pixels = {}, {}, []
    for name, mask in masks.items():
        ys = np.full(len(xs), np.nan)
        for k, x in enumerate(xs):
            candidates = np.flatnonzero(mask[top:bottom+1, x]) + top
            if len(candidates):
                # Hue masks separate colored strokes from grey axes/grid/text.
                # Saturation/brightness weights suppress faint antialias halos.
                weights = s[candidates, x] * v[candidates, x]
                order = np.argsort(candidates)
                cumulative = np.cumsum(weights[order])
                ys[k] = candidates[order][np.searchsorted(cumulative, cumulative[-1]/2)]
                pixels.append((case, name, int(x), float(ys[k])))
        good = np.isfinite(ys)
        if good.sum() < 20:
            raise ValueError(f'not enough visible pixels for {case}/{name}')
        interp_y = np.interp(sample_x, xs[good], ys[good])
        traces[name] = np.round((zero_y-interp_y)*value_per_pixel, 6)
        gaps = np.diff(np.r_[left-1, xs[good], right+1])-1
        quality[name] = {'observed_columns': int(good.sum()),
                         'missing_columns': int((~good).sum()),
                         'largest_interpolated_gap_pixels': int(gaps.max()),
                         'min': float(traces[name].min()), 'max': float(traces[name].max())}
    stamps = [origin_time+timedelta(seconds=int(t)) for t in seconds]
    metadata = {'step_seconds': step, 'unit': unit, 'points_per_trace': len(stamps),
                'start': stamps[0].isoformat(), 'end': stamps[-1].isoformat(),
                'traces': list(traces), 'quality': quality,
                'calibration': {'origin_x': origin_x, 'origin_time': origin_time.isoformat(),
                    'seconds_per_pixel': seconds_per_pixel, 'zero_y': zero_y,
                    'value_per_pixel': value_per_pixel}}
    return stamps, traces, metadata, pixels


def main():
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT/'source').mkdir(exist_ok=True)
    rows, lines, pixel_rows, metadata = [], [], [], {}
    fig, axes = plt.subplots(2, 1, figsize=(17, 10), layout='constrained')
    for ax, (case, path) in zip(axes, SOURCES.items()):
        saved_source = OUTPUT/'source'/f'{case}.png'
        if saved_source.exists():
            path = saved_source
        stamps, traces, meta, pixels = extract(path, case)
        if path.resolve() != saved_source.resolve():
            shutil.copyfile(path, saved_source)
        meta['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        metadata[case] = meta
        pixel_rows.extend(pixels)
        for name, values in traces.items():
            ax.plot(stamps, values, color=COLORS[name], label=name, lw=1)
            for stamp, value in zip(stamps, values):
                rows.append((stamp.isoformat(), case, name, f'{value:.6f}', int(stamp.timestamp())))
                lines.append(f'correlation_screenshot_replay,scenario={case},trace={name} value={value:.6f} {int(stamp.timestamp())}')
        ax.set_title(case + ' — screenshot reconstruction')
        ax.set_ylabel(meta['unit'])
        ax.legend(ncol=len(traces))
        ax.grid(alpha=.25)
        import matplotlib.dates as mdates
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m %H:%M', tz=MSK))
    fig.savefig(OUTPUT/'preview.png', dpi=140)
    plt.close(fig)
    with (OUTPUT/'metrics.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(('timestamp','scenario','trace','value','unix_seconds'))
        writer.writerows(rows)
    with (OUTPUT/'pixels.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(('scenario','trace','x','y'))
        writer.writerows(pixel_rows)
    (OUTPUT/'metrics.lp').write_text('\n'.join(lines)+'\n', encoding='utf-8', newline='\n')
    result = {'measurement': 'correlation_screenshot_replay', 'bucket': 'metrics-replay',
              'organization': 'atropos', 'timezone': 'Europe/Moscow',
              'is_raw_telemetry': False, 'points': len(rows), 'scenarios': metadata,
              'csv_sha256': hashlib.sha256((OUTPUT/'metrics.csv').read_bytes()).hexdigest(),
              'limitations': ['Approximate raster reconstruction, not original telemetry.',
                'Only three visible latency curves and four separately visible read-operation curves are recoverable.',
                'Fully hidden legend traces cannot be recovered and are not fabricated.',
                'Occluded columns are linearly interpolated; edge gaps use nearest visible value.',
                'Raster noise, overlap and narrow peaks limit correlation accuracy; no synthetic noise added.',
                'Read graph starts 18 October and crosses midnight into 19 October 2026.',
                'Latency y axis in ms; read-operation axis tick labels are 100..900 ops/s.']}
    (OUTPUT/'metadata.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
