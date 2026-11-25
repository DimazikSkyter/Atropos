"""Approximate the green trace from the supplied screenshot, not raw telemetry."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image


def digitize(source: Path, destination: Path, units_per_pixel=1.0):
    pixels = np.asarray(Image.open(source).convert('RGB'), dtype=float)
    red, green, blue = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
    mask = ((green > red * 1.20) & (green > blue * 1.06)
            & (green - red > 10) & (green > 38))
    # Only the green series. The red annotation and yellow line are excluded.
    mask[:100] = False
    mask[690:] = False
    strength = np.where(mask, green - (red + blue) / 2, 0.0)
    columns = np.arange(64, 1531)
    trace = np.full(len(columns), np.nan)
    envelope = np.full(len(columns), np.nan)
    spans = np.zeros(len(columns), dtype=int)
    for index, x in enumerate(columns):
        column = strength[:, x]
        rows = np.flatnonzero(column > 0)
        if not len(rows):
            continue
        spans[index] = int(rows[-1] - rows[0])
        # An alternative that keeps thin downward spikes. Such a lower
        # envelope can also include connecting strokes, so retain both reads.
        envelope[index] = np.percentile(rows, 98)
        # Locate the strongest local line, not the bottom of a vertical
        # connector. A connector does not reveal the underlying sample value.
        center = int(np.argmax(np.convolve(column, np.ones(7), mode='same')))
        neighborhood = np.arange(max(100, center - 4), min(690, center + 5))
        weights = column[neighborhood]
        trace[index] = np.average(neighborhood, weights=weights)
    known = np.isfinite(trace)
    trace = np.interp(columns, columns[known], trace[known])
    envelope = np.interp(columns, columns[known], envelope[known])
    # Read horizontal time tick positions: 13:00 at x=64, next day's 10:00
    # at x=1493. No date is inferred from this screenshot.
    pixels_per_minute = (1493 - 64) / (21 * 60)
    minutes = np.arange(int((1530 - 64) / pixels_per_minute) + 1)
    sample_x = 64 + minutes * pixels_per_minute
    sample_y = np.interp(sample_x, columns, trace)
    sample_envelope_y = np.interp(sample_x, columns, envelope)
    baseline_y = float(np.median(trace[columns < 170]))
    envelope_baseline_y = float(np.median(envelope[columns < 170]))
    center_values = 1000.0 + (baseline_y - sample_y) * units_per_pixel
    values = 1000.0 + (envelope_baseline_y - sample_envelope_y) * units_per_pixel
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('w', encoding='utf-8', newline='') as target:
        writer = csv.writer(target)
        writer.writerow(['minute', 'value', 'center_value', 'pixel_x',
                         'envelope_pixel_y', 'center_pixel_y'])
        writer.writerows(zip(minutes, values, center_values, sample_x,
                             sample_envelope_y, sample_y))
    with destination.with_name('real-system-pixels.csv').open('w', encoding='utf-8', newline='') as target:
        writer = csv.writer(target)
        writer.writerow(['pixel_x', 'center_pixel_y', 'envelope_pixel_y',
                         'green_span', 'interpolated_column'])
        writer.writerows(zip(columns, trace, envelope, spans, (~known).astype(int)))
    metadata = {
        'source': source.name,
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'method': 'green-channel dominant center and 98th-percentile lower envelope; one-minute sampling',
        'is_raw_telemetry': False,
        'confirmed_parameters': {'baseline_value': 1000.0, 'sample_step_seconds': 60},
        'assumptions': {'units_per_vertical_pixel': units_per_pixel,
                        'baseline_pixel_y': baseline_y,
                        'envelope_baseline_pixel_y': envelope_baseline_y,
                        'time_tick_13_00_x': 64, 'time_tick_next_day_10_00_x': 1493},
        'points': len(values),
        'interpolated_columns': int(np.sum(~known)),
        'columns_with_green_span_over_30px': int(np.sum(spans > 30)),
        'first_dip_minutes': [105, 155],
        'long_plateau_minutes': [665, 795],
        'long_plateau_core_minutes': [680, 780],
        'limitations': ['Y scale is unknown; depth is conditional on units_per_vertical_pixel',
                        'Dominant line may not preserve narrow spikes or vertical connectors',
                        'Lower envelope may interpret connecting strokes as narrow spikes',
                        'Pixel noise and original sample noise cannot be separated',
                        'Original timestamps and values are unavailable'],
    }
    destination.with_suffix('.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2),
                                               encoding='utf-8')
    return metadata


def main():
    fixtures = Path(__file__).with_name('fixtures')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=fixtures / 'real-system-source.png')
    parser.add_argument('--output', type=Path, default=fixtures / 'real-system-replay.csv')
    parser.add_argument('--units-per-pixel', type=float, default=1.0)
    args = parser.parse_args()
    if not np.isfinite(args.units_per_pixel) or args.units_per_pixel <= 0:
        parser.error('--units-per-pixel must be finite and positive')
    print(json.dumps(digitize(args.source, args.output, args.units_per_pixel),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
