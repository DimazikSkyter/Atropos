"""Headless visual fixtures: uv run python -m tests.visualize_generator."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
from matplotlib import pyplot as plt

from apps.generator import (
    AnomalyGenerator, CycleConfig, MetricResult, TimeSeriesGenerator,
    configure_json_logging,
)


MOSCOW = timezone(timedelta(hours=3))
START = datetime(2026, 10, 4, tzinfo=MOSCOW)


def scenarios():
    base = TimeSeriesGenerator(name='request_rate', mean=4300, start_time=START, seed=42)
    original = base.generate(960)
    yield 'Stationary baseline: mixed noise, rare 1-3 point spikes', original, original, None
    cyclic = TimeSeriesGenerator(name='latency', mean=270, start_time=START, seed=42,
                cycles=(CycleConfig(120, 2), CycleConfig(32, 0.5), CycleConfig(8, 0.2))).generate(960)
    yield 'Three cycles: periods 120 / 32 / 8 points, amplitudes 2 / 0.5 / 0.2%', cyclic, cyclic, None
    for title, start, stop, transform in (
        ('Injected 3-point spike: +50%', 400, 403, lambda value: value * 1.5),
        ('Injected temporary level shift: +30%', 400, 520, lambda value: value * 1.3),
        ('Injected drop: -40%', 600, 680, lambda value: value * 0.6),
    ):
        modified = AnomalyGenerator(original, start, stop, transform).generate()
        yield title, original, modified, (start, stop)


def metric_dict(metric: MetricResult):
    return {'name': metric.name, 'tags': metric.tags,
            'values': metric.values, 'timestamps': metric.timestamps}


def create_report(output: Path) -> Path:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cases = list(scenarios())
    fig, axes = plt.subplots(len(cases), 1, figsize=(15, 16), layout='constrained')
    fig.suptitle('Synthetic metrics and anomalies | 960 points | 15-second step', fontsize=16)
    rows = []
    try:
        for ax, (title, original, modified, interval) in zip(axes, cases):
            dates = [datetime.fromtimestamp(t, MOSCOW) for t in original.timestamps]
            ax.plot(dates, original.values, color='#3b82f6', linewidth=0.9, label='Original')
            if interval is not None:
                start, stop = interval
                segment = slice(max(0, start - 1), min(len(dates), stop + 1))
                ax.plot(dates[segment], modified.values[segment], color='#ed6c35',
                        linewidth=0.9, label='With anomaly')
                # The stop timestamp is outside the anomaly, matching [start, stop).
                end = datetime.fromtimestamp(original.timestamps[stop - 1] + 15, MOSCOW)
                ax.axvspan(dates[start], end, color='#ed6c35', alpha=0.15)
            ax.set_title(title, loc='left', fontsize=12)
            ax.set_ylabel('req/s' if original.name == 'request_rate' else 'ms')
            ax.set_ylim(bottom=0)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=MOSCOW))
            ax.set_xlabel('2026-10-04, UTC+03:00')
            ax.grid(alpha=0.2)
            ax.legend(loc='upper right')
            rows.append({'title': title, 'anomaly_range': interval,
                         'original': metric_dict(original), 'anomalous': metric_dict(modified)})
        fig.savefig(output / 'report.png', dpi=120)
    finally:
        plt.close(fig)
    (output / 'series.json').write_text(json.dumps({'scenarios': rows}, ensure_ascii=False,
                                                allow_nan=False, indent=2), encoding='utf-8')
    return output / 'report.png'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/generator'))
    args = parser.parse_args()
    configure_json_logging()
    create_report(args.output)


if __name__ == '__main__':
    main()
