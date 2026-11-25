"""Six baselines and two anomalies: uv run python -m tests.visualize_correlation."""

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
import numpy as np

from apps.correlation import RollingCorrelationAnalyzer
from apps.generator import (
    AnomalyGenerator, CycleConfig, NoiseConfig, SpikeConfig, TimeSeriesGenerator,
    configure_json_logging,
)


POINTS = 200
INPUT_WINDOW = 20
MIDPOINT = POINTS // 2
BURST_MULTIPLIER = 1.2
BURST_DURATION_POINTS = 15
TREND_ANGLE_DEGREES = 10.0
TREND_SLOPE_PER_POINT = float(np.tan(np.deg2rad(TREND_ANGLE_DEGREES)))
TREND_NOISE_VARIANCE_MULTIPLIER = 2.0
BASE_ANOMALY_NOISE = NoiseConfig()
_TREND_STD_MULTIPLIER = float(np.sqrt(TREND_NOISE_VARIANCE_MULTIPLIER))
TREND_NOISE = replace(
    BASE_ANOMALY_NOISE,
    white_std_percent=BASE_ANOMALY_NOISE.white_std_percent * _TREND_STD_MULTIPLIER,
    correlated_std_percent=BASE_ANOMALY_NOISE.correlated_std_percent * _TREND_STD_MULTIPLIER,
    heavy_tail_std_percent=BASE_ANOMALY_NOISE.heavy_tail_std_percent * _TREND_STD_MULTIPLIER,
)
HEAVY_NOISE = NoiseConfig(3.5, 4, 0.65, 1)
BASE_SPECS = (
    ('white-noise', 'No cycle: white noise', NoiseConfig(1, 0, 0, 0), (), 41,
     SpikeConfig(probability=0)),
    ('mixed-noise', 'No cycle: stationary mixed noise', NoiseConfig(), (), 42,
     SpikeConfig(probability=0)),
    ('short-cycle', 'One short cycle: 10 points', NoiseConfig(),
     (CycleConfig(10, 5),), 43, SpikeConfig(probability=0)),
    ('two-cycles', 'Two cycles: 10 and 100 points', NoiseConfig(),
     (CycleConfig(10, 3), CycleConfig(100, 5)), 44, SpikeConfig(probability=0)),
    ('high-noise', 'Generated stationary series: 10x mixed noise, no cycles',
     HEAVY_NOISE, (), 46, SpikeConfig()),
    ('high-noise-cycle', 'Generated 10-point cycle: 10x mixed noise',
     HEAVY_NOISE, (CycleConfig(10, 5),), 43, SpikeConfig()),
)


def scenarios():
    for slug, title, noise, cycles, seed, spikes in BASE_SPECS:
        metric = TimeSeriesGenerator(name=slug, mean=100, noise=noise, cycles=cycles,
                                     spikes=spikes, seed=seed, start_time=0).generate(POINTS)
        yield slug, title, metric, [c.period_points for c in cycles]


def anomaly_scenarios():
    """Construct fresh callbacks and generator snapshots on every fixture call.

    The numeric angle uses point indices and metric-value units, not display pixels.
    Point 100 starts the continuous ramp with a zero increment; point 199 has
    increment 99 * tan(10 degrees). Both fixtures use the same seeded random
    innovations; the trend fixture scales noise std by sqrt(2), variance by 2.
    """
    for slug, title in (
        ('trend-10-degrees', 'Stationary -> trend at point 100: slope tan(10 deg)\nNoise variance x2'),
        ('burst-20-percent', 'Stationary -> +20% burst on [100, 115) -> full recovery'),
    ):
        noise = TREND_NOISE if slug == 'trend-10-degrees' else BASE_ANOMALY_NOISE
        original = TimeSeriesGenerator(name=slug, mean=100, noise=noise, seed=47,
                                       start_time=0).generate(POINTS)
        if slug == 'trend-10-degrees':
            # The value-only callback consumes successive offsets. A fresh iterator
            # is created for this single injection, so repeated fixture calls match.
            offsets = iter(np.arange(POINTS - MIDPOINT) * TREND_SLOPE_PER_POINT)
            metric = AnomalyGenerator(original, MIDPOINT, POINTS,
                                      lambda value: value + float(next(offsets))).generate()
            metadata = {'anomaly_kind': 'trend', 'anomaly_range': [MIDPOINT, POINTS],
                        'angle_degrees': TREND_ANGLE_DEGREES,
                        'slope_per_point': TREND_SLOPE_PER_POINT,
                        'angle_axes': 'point_index, metric_value'}
        else:
            stop = MIDPOINT + BURST_DURATION_POINTS
            metric = AnomalyGenerator(original, MIDPOINT, stop,
                                      lambda value: value * BURST_MULTIPLIER).generate()
            metadata = {'anomaly_kind': 'burst', 'anomaly_range': [MIDPOINT, stop],
                        'multiplier': BURST_MULTIPLIER, 'duration_points': BURST_DURATION_POINTS}
        metadata.update({'noise': asdict(noise), 'noise_variance_multiplier':
                         TREND_NOISE_VARIANCE_MULTIPLIER if slug == 'trend-10-degrees' else 1.0})
        yield slug, title, original, metric, metadata


def _render_report(cases, output: Path, filename: str, title: str):
    fig, axes = plt.subplots(len(cases), 3, figsize=(17, 3.25 * len(cases)),
                             squeeze=False, layout='constrained',
                             gridspec_kw={'width_ratios': (1.35, 1, 1)})
    fig.suptitle(title, fontsize=16)
    summary = []
    try:
        for axis_row, case in zip(axes, cases):
            slug, metric, original = case['slug'], case['metric'], case['original']
            result = RollingCorrelationAnalyzer(window=INPUT_WINDOW).analyze(metric)
            attrs = result.values.attrs
            starts = np.asarray(result.timestamps) // 15
            row = {'scenario': slug, 'group': case['group'],
                   'expected_periods': case['periods'],
                   'detected_periods': attrs['periods'], 'points': len(metric.values),
                   'input_window': INPUT_WINDOW, 'window': result.window, 'step': attrs['step'],
                   'max_lag': attrs['max_lag'], 'windows': len(result.values),
                   'first_start': int(starts[0]), 'last_start': int(starts[-1]),
                   'last_stop': int(starts[-1] + result.window), **case['metadata']}
            summary.append(row)
            ax = axis_row[0]
            if original is None:
                ax.plot(metric.values, color='#2563eb', linewidth=1)
                ax.axvspan(0, result.window, color='#0891b2', alpha=0.12,
                           label='First window')
            else:
                ax.plot(original.values, color='#2563eb', linewidth=1, label='Original')
                start, stop = case['metadata']['anomaly_range']
                segment = slice(max(0, start - 1), min(POINTS, stop + 1))
                ax.plot(np.arange(POINTS)[segment], metric.values[segment],
                        color='#ea580c', linewidth=1.2, label='With anomaly')
                ax.axvspan(start, stop, color='#ea580c', alpha=0.1)
                ax.axvline(start, color='#ea580c', linewidth=0.8, linestyle='--')
                if case['metadata']['anomaly_kind'] == 'trend':
                    trend = 100 + np.arange(POINTS - start) * TREND_SLOPE_PER_POINT
                    ax.plot(np.arange(start, POINTS), trend, linestyle=':', color='#334155',
                            linewidth=1, label='Expected level + ramp')
            ax.set_title(case['title'] + f"\nDetected periods: {attrs['periods']}",
                                  fontsize=11, loc='left')
            ax.set_xlabel('Point index')
            ax.set_ylabel('Value')
            ax.set_ylim(bottom=0)
            ax.grid(alpha=0.2)
            ax.legend(loc='upper left' if original is not None else 'upper right', fontsize=8)
            for ax, kind in zip(axis_row[1:], ('acf', 'pacf')):
                data = result.values[kind].loc[:, 1:].to_numpy().T
                mesh = ax.pcolormesh(np.arange(len(starts) + 1),
                                     np.arange(1, attrs['max_lag'] + 2) - 0.5, data,
                                     cmap='RdBu_r', vmin=-1, vmax=1, shading='flat')
                # Labels identify actual source indices rather than treating timestamps
                # as consecutive raw observations. Every cell represents a full window.
                ticks = np.unique(np.linspace(0, len(starts) - 1, 5, dtype=int))
                ax.set_xticks(ticks + 0.5, [str(starts[t]) for t in ticks])
                ax.set_yticks(range(1, attrs['max_lag'] + 1))
                ax.set_xlabel('Window start (point index)')
                ax.set_ylabel('Lag (points)')
                ax.set_title(f'{kind.upper()} | W={result.window}, step={attrs["step"]}, '
                             f'lags=1..{attrs["max_lag"]}', fontsize=11)
                fig.colorbar(mesh, ax=ax, shrink=0.82)
            result.values.to_csv(output / f'{slug}-correlations.csv')
            np.savetxt(output / f'{slug}-series.csv',
                       np.column_stack((metric.timestamps, metric.values)), delimiter=',',
                       header='timestamp,value', comments='')
            if original is not None:
                np.savetxt(output / f'{slug}-original.csv',
                           np.column_stack((original.timestamps, original.values)), delimiter=',',
                           header='timestamp,value', comments='')
        fig.savefig(output / filename, dpi=120)
    finally:
        plt.close(fig)
    return summary


def create_report(output: Path) -> Path:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    specs = {slug: (noise, seed, spikes) for slug, _, noise, _, seed, spikes in BASE_SPECS}
    base_cases = []
    for slug, title, metric, periods in scenarios():
        noise, seed, spikes = specs[slug]
        base_cases.append({'slug': slug, 'title': title, 'metric': metric, 'original': None,
                           'periods': periods, 'group': 'baseline',
                           'metadata': {'noise': asdict(noise), 'seed': seed,
                                        'spikes': asdict(spikes)}})
    anomaly_cases = [
        {'slug': slug, 'title': title, 'metric': metric, 'original': original,
         'periods': [], 'group': 'anomaly', 'metadata': {**metadata, 'seed': 47,
             'spikes': asdict(SpikeConfig())}}
        for slug, title, original, metric, metadata in anomaly_scenarios()
    ]
    summary = _render_report(base_cases, output, 'report.png',
                             'Baseline ACF / PACF | 200 points per series | input window 20')
    summary += _render_report(anomaly_cases, output, 'anomalies.png',
                              'Trend and burst ACF / PACF | 200 points | input window 20')
    (output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return output / 'report.png'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/correlation'))
    args = parser.parse_args()
    configure_json_logging()
    create_report(args.output)


if __name__ == '__main__':
    main()
