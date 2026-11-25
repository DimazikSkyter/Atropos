"""Exact signal construction/seed from AnomalyDetectionResolutionSystem CUSUM_TEST.py."""

import numpy as np


def real_system_scenario():
    """One-minute approximation digitized from the supplied green screenshot trace."""
    import json
    from pathlib import Path

    fixture = Path(__file__).with_name('fixtures') / 'real-system-replay.csv'
    data = np.genfromtxt(fixture, delimiter=',', names=True)
    metadata = json.loads(fixture.with_suffix('.json').read_text(encoding='utf-8'))
    return data, metadata


def legacy_scenarios():
    noise = np.random.RandomState(43).rand(1000) * 2 - 1
    specs = (
        ('short', 492, [86, 76, 50, 85, 49, 52], 393),
        ('medium', 490, [86, 76, 50, 53, 51, 52, 49, 52, 51, 55, 58, 51, 57, 60,
                         67, 71, 82, 95], 383),
        ('long', 490, [86, 76, 50, 53, 51, 52, 49, 37, 28, 29, 40, 42, 39, 52,
                       61, 70, 74, 51, 57, 60, 67, 71, 82, 95, 84, 87, 92, 97], 373),
        ('shallow', 492, [89, 97, 95, 94, 96, 99, 89, 97, 95, 94], 389),
    )
    for name, plateau, burst, tail in specs:
        base = list(range(100)) + [100] * plateau + burst + [100] * tail + list(range(90, 0, -10))
        yield name, noise + base, 100 + plateau, 100 + plateau + len(burst)


def saw_scenario(noise_std=0.4, seed=43):
    """Stylized first screenshot, not pixel-extracted telemetry."""
    rng = np.random.default_rng(seed)
    values = 100 + rng.normal(0, noise_std, 1000)
    values[300:800] += 100  # A level change is not itself a burst.
    for start in range(350, 650, 6):
        values[start:start + 4] += [0, -4, -9, -3]
    return values


def noisy_scenario(seed=43):
    """A variable background with the same excursions as the quiet saw."""
    return saw_scenario(noise_std=12, seed=seed)


def small_dip_scenario(repeats=1):
    """The same five-percent dip, isolated or repeated, on a horizontal baseline."""
    values = np.full(1000, 100.0)
    for start in range(350, 350 + repeats * 5, 5):
        values[start + 1] = 95.0
    return values


def legacy_scores(values, threshold=1.0, drift=0.01):
    """Original multiplier, smoothing, reset and normalization for comparison only."""
    from collections import deque
    import math

    data = (np.asarray(values) - np.min(values)) / np.ptp(values)
    deltas = deque([0.0] * 4, maxlen=4)
    positive = negative = 0.0
    scores = []
    for i, value in enumerate(data):
        delta = 0.8 * (value - data[i - 1]) + 0.2 * sum(deltas)
        deltas.append(delta)
        positive = max(0.0, positive + delta - drift)
        negative = max(0.0, negative - delta - drift)
        product = 25 * positive * negative
        scores.append(math.tanh(product / (2 * threshold)))
        if product > threshold:
            positive = negative = 0.0
    return np.asarray(scores)
