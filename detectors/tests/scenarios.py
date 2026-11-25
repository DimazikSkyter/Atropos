"""Deterministic synthetic gauges; timestamps in seconds, scrape interval 15s.

CPU is in percentage points, response time in milliseconds, GC in arbitrary units.
These fixtures model behavior, not measured production distributions.
"""
import math
import random


def gc_saw(minutes=90, drift=0.0):
    rng = random.Random(101)
    return [(i * 15.0, 100 + drift * i / 4 +
             25 * (i % 20) / 20 + 8 * (i % 7) / 7 + rng.gauss(0, 0.6))
            for i in range(minutes * 4 + 1)]


def cpu_load(minutes=30):
    rng = random.Random(202)
    result = []
    for i in range(minutes * 4 + 1):
        t = i / 4
        baseline = 20 + 6 * min(10, max(0, t - 5))
        value = baseline + 10 * math.sin(2 * math.pi * i / 5) + rng.gauss(0, 3)
        result.append((i * 15.0, min(100, max(0, value))))
    return result


def response(kind, minutes=20, seed=303):
    rng = random.Random(seed)
    result = []
    for i in range(minutes * 4 + 1):
        t = i / 4
        base = 20.0
        if kind == 'spike':
            base += 180 if i in (24, 25) else 0
        elif kind == 'step':
            base += 40 if t >= 6 else 0
        elif kind == 'temporary_step':
            base += 40 if 6 <= t < 10 else 0
        elif kind == 'backpressure':
            base += 12 * max(0, min(t - 4, 4)) - 12 * max(0, min(t - 8, 4))
        elif kind == 'quiet_rise':
            base += 2 * max(0, t - 4)
        elif kind != 'flat':
            raise ValueError(kind)
        result.append((i * 15.0, base + rng.gauss(0, 0.025)))
    return result


def dirty_latency(minutes=60, seed=404):
    rng = random.Random(seed)
    result = []
    for i in range(minutes * 4 + 1):
        # Stationary distribution with no deterministic drift, high relative noise.
        value = rng.lognormvariate(math.log(0.2), 0.8)
        if rng.random() < 0.10:
            value += rng.uniform(1, 5)
        result.append((i * 15.0, value))
    return result
