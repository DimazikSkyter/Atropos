"""Reuse the actual fixtures behind all five earlier artifact reports.

No previous detector's scores are labels for prediction-interval correctness.
All streams are synthetic, except the explicitly labelled screenshot
reconstruction, which is also not raw production telemetry.
"""

from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np

from tests.acf_scenarios import persistence_scenarios
from tests.cusum_scenarios import (
    legacy_scenarios, noisy_scenario, real_system_scenario,
    saw_scenario, small_dip_scenario,
)
from tests.scenarios import cpu_load, dirty_latency, gc_saw, response


@dataclass(frozen=True)
class IntervalScenario:
    name: str
    label: str
    values: np.ndarray
    source: str
    start: int | None = None
    end: int | None = None
    window: int = 30
    scale: float = 1.0


def reconstructed_rps_15s():
    """Reproduce infra/influxdb/cusum-rps/generate.py from existing pixels."""
    fixtures = Path(__file__).with_name('fixtures')
    pixels = np.genfromtxt(fixtures / 'real-system-pixels.csv', delimiter=',', names=True)
    assumptions = json.loads((fixtures / 'real-system-replay.json').read_text(
        encoding='utf-8'))['assumptions']
    x_start = assumptions['time_tick_13_00_x']
    pixels_per_second = (assumptions['time_tick_next_day_10_00_x'] - x_start) / (21*3600)
    duration = (pixels['pixel_x'][-1] - x_start) / pixels_per_second
    x = x_start + np.arange(0, int(duration)+1, 15) * pixels_per_second
    y = np.interp(x, pixels['pixel_x'], pixels['envelope_pixel_y'])
    values = 1000 + (assumptions['envelope_baseline_pixel_y'] - y) * assumptions['units_per_vertical_pixel']
    return np.round(values, 6)


def previous_report_scenarios():
    for name, label, _, values, start, end in persistence_scenarios():
        yield IntervalScenario('acf-' + name, label, values, 'acf', start, end)
    for name, values, start, end in legacy_scenarios():
        yield IntervalScenario('cusum-' + name, 'CUSUM: ' + name, values, 'cusum', start, end)
    for name, label, values, start, end in (
        ('quiet-saw', 'Пила на тихом фоне', saw_scenario(), 350, 650),
        ('noisy-saw', 'Та же пила на шумном фоне', noisy_scenario(), 350, 650),
        ('isolated-small-dip', 'Одиночный провал 5%', small_dip_scenario(), 351, 352),
        ('repeated-small-dip', 'Повторяющийся провал 5%', small_dip_scenario(50), 350, 600),
        ('constant', 'Постоянный уровень', np.full(1000, 100.0), None, None),
        ('level-step', 'Смена постоянного уровня', np.r_[np.full(500, 100.0),
                                                       np.full(500, 200.0)], 500, 1000),
    ):
        yield IntervalScenario('cusum-' + name, label, values, 'cusum', start, end)
    reconstruction, _ = real_system_scenario()
    yield IntervalScenario('cusum-image-reconstruction', 'Реконструкция скриншота, шаг 1 мин',
                           reconstruction['value'], 'cusum/real-system-image')
    yield IntervalScenario('cusum-rps-15s', 'Тот же скриншот: RPS, шаг 15 секунд',
                           reconstructed_rps_15s(), 'cusum/real-system-rps-15s')

    runtime = [('gc', 'GC: две пилы', gc_saw()),
               ('gc-drift', 'GC: две пилы и дрейф', gc_saw(drift=1.5)),
               ('cpu', 'CPU: рост 20 → 80%', cpu_load()),
               ('dirty-latency', 'Задержки с тяжёлым хвостом', dirty_latency())]
    runtime += [('response-' + kind, 'Response: ' + kind, response(kind))
                for kind in ('flat', 'quiet_rise', 'spike', 'step', 'temporary_step', 'backpressure')]
    for name, label, samples in runtime:
        yield IntervalScenario('runtime-' + name, label, np.array([v for _, v in samples]),
                               'runtime-trend', window=8 if name.startswith('response-') else 30)

    # These are the same seeded generators used to create the exported CSVs.
    from tests.visualize_correlation import anomaly_scenarios, scenarios
    for name, label, metric, _ in scenarios():
        yield IntervalScenario('correlation-' + name, label, np.array(metric.values), 'correlation')
    for name, label, _, metric, metadata in anomaly_scenarios():
        start, end = metadata['anomaly_range']
        yield IntervalScenario('correlation-' + name, label, np.array(metric.values),
                               'correlation', start, end)

    from tests.visualize_generator import scenarios as generator_scenarios
    for index, (label, original, modified, interval) in enumerate(generator_scenarios()):
        metric = modified if modified is not None else original
        start, end = interval if interval is not None else (None, None)
        yield IntervalScenario(f'generator-{index}', label, np.array(metric.values),
                               'generator', start, end)

    # Existing MAD fixtures: a step and then a progressively increasing std.
    rng = np.random.default_rng(43)
    yield IntervalScenario('mad-variance-step', 'MAD: стандартное отклонение 0.1 → 3',
                           np.r_[rng.normal(0, 0.1, 500), rng.normal(0, 3, 500)], 'tests/test_mad.py',
                           500, 1000)
    rng = np.random.RandomState(43)
    yield IntervalScenario('mad-variance-gradual', 'MAD: постепенный рост разброса',
                           np.r_[rng.normal(0, 0.1, 1000),
                                 rng.normal(0, (np.arange(1000)+10)/100)], 'tests/test_mad.py',
                           1000, 2000)


def stability_scenarios(seed=43):
    """High mean with weak cycles, both white noise and a true MA(8) process."""
    count = 1800
    rng = np.random.default_rng(seed)
    cycle = 0.02 * np.sin(2*np.pi*np.arange(count)/16)
    yield IntervalScenario(f'stability-high-mean-{seed}', f'Высокий уровень, слабый цикл; seed={seed}',
                           1000 + cycle + rng.normal(0, 0.05, count), 'new stability controls')
    innovations = rng.normal(size=count+8)
    ma = np.convolve(innovations, np.ones(9)/3, mode='valid')
    yield IntervalScenario(f'stability-ma8-{seed}', f'MA(8), высокий уровень, слабый цикл; seed={seed}',
                           1000 + cycle + 0.05*ma, 'new stability controls')
