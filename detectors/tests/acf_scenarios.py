"""Deterministic persistence fixtures; no external telemetry or future values."""

import numpy as np


def ar_series(phi, count, seed=43):
    rng = np.random.default_rng(seed)
    noise = rng.normal(size=count + 2000)
    values = np.zeros(len(noise))
    for i in range(1, len(values)):
        values[i] = phi * values[i - 1] + noise[i]
    return values[-count:]


def cycle_change_scenario(kind, seed=43):
    """Controlled cycle changes with continuous phase and a healthy recovery.

    The mixed case is inspired by the supplied chart: initially regular cycles
    acquire a changing period and growing amplitude. Units/time are synthetic.
    """
    rng = np.random.default_rng(seed)
    count, start, end = 1800, 450, 1050
    omega = np.full(count, 2*np.pi/16)
    amplitude = np.ones(count)
    names = {
        'slower': ('cycle_slower', 'Период цикла 16 → 32 → 16'),
        'faster': ('cycle_faster', 'Период цикла 16 → 8 → 16'),
        'irregular': ('cycle_irregular', 'Нарушение регулярности: периоды 8, 12, 28, 40'),
        'gradual': ('cycle_gradual', 'По примеру: изменение периода и рост амплитуды'),
        'amplitude': ('cycle_amplitude', 'Контроль: растет только амплитуда цикла'),
    }
    if kind not in names:
        raise ValueError(f'Unknown cycle scenario: {kind}')
    if kind in ('slower', 'faster'):
        omega[start:end] = 2*np.pi/(32 if kind == 'slower' else 8)
    elif kind == 'irregular':
        index = start
        while index < end:
            period = int(rng.choice((8, 12, 28, 40)))
            stop = min(end, index+period)
            omega[index:stop] = 2*np.pi/period
            index = stop
    elif kind == 'gradual':
        progress = np.linspace(0, 1, end-start)
        periods = 16 + 16*progress + 2*progress*np.sin(2*np.pi*progress*5)
        omega[start:end] = 2*np.pi/periods
        amplitude[start:end] = 1 + 2.5*progress
    else:
        amplitude[start:end] = 3.5
    phase = np.cumsum(omega)-omega[0]
    # Scale the entire noisy waveform in the amplitude-only control to keep SNR.
    values = amplitude*(np.sin(phase) + rng.normal(0, 0.05, count))
    healthy = np.sin(2*np.pi*np.arange(512)/16) + rng.normal(0, 0.05, 512)
    name, label = names[kind]
    return name, label, healthy, values, start, end


def persistence_scenarios():
    """name, Russian label, healthy history, stream, episode [start, end)."""
    rng = np.random.default_rng(43)
    reference = rng.normal(0, 0.05, 512)
    for duration in (1, 4, 16, 64, 400):
        values = rng.normal(0, 0.05, 1200)
        values[350:350 + duration] += 3
        yield (f'plateau_{duration}', f'Воздействие {duration} отсчётов',
               reference.copy(), values, 350, 350 + duration)
    for tau in (2, 8, 24):
        values = rng.normal(0, 0.05, 1200)
        values[350:650] += 3 * np.exp(-np.arange(300) / tau)
        yield (f'recovery_{tau}', f'Восстановление, tau={tau}',
               reference.copy(), values, 350, 650)
    yield ('white_noise', 'Белый шум без изменения', reference.copy(),
           rng.normal(0, 0.05, 1200), 350, 750)
    values = rng.normal(0, 0.05, 1200)
    values[350:750] *= 30
    yield ('variance_only', 'Только увеличение разброса', reference.copy(),
           values, 350, 750)
    yield ('ar_unchanged', 'Обычная память AR(0.8)', ar_series(0.8, 512),
           ar_series(0.8, 1200, 55), 350, 750)
    values = np.r_[rng.normal(0, 0.05, 350), ar_series(0.9, 400) * 0.05,
                   rng.normal(0, 0.05, 450)]
    yield ('ar_new', 'Новая память AR(0.9)', reference.copy(), values, 350, 750)
    values = np.r_[ar_series(0.85, 350, 55), rng.normal(size=400),
                   ar_series(0.85, 450, 77)]
    yield ('ar_loss', 'Исчезновение памяти AR(0.85)', ar_series(0.85, 512),
           values, 350, 750)
    phase = np.arange(1200)
    periodic = np.sin(2 * np.pi * phase / 16)
    healthy = np.sin(2 * np.pi * np.arange(512) / 16)
    yield ('periodic_unchanged', 'Обычный цикл периода 16', healthy,
           periodic, 350, 750)
    values = rng.normal(0, 0.05, 1200)
    values[350:750] += periodic[350:750]
    yield ('periodic_new', 'Новый цикл периода 16', reference.copy(),
           values, 350, 750)
    for kind in ('slower', 'faster', 'irregular', 'gradual', 'amplitude'):
        yield cycle_change_scenario(kind)
