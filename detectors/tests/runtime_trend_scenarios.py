"""Trend report fixtures, including unchanged signals reused from Bond tests."""

from dataclasses import dataclass

from apps.runtime_trend import TrendConfig
from tests.scenarios import cpu_load, dirty_latency, gc_saw, response


@dataclass(frozen=True)
class Span:
    start: float
    end: float
    label: str
    direction: str = 'none'


@dataclass(frozen=True)
class TrendScenario:
    slug: str
    name: str
    label: str
    points: tuple[tuple[float, float], ...]
    config: TrendConfig
    source: str
    unit: str
    expectation: str
    criterion: str = 'observe'
    events: tuple[Span, ...] = ()
    # None: unlabelled. Empty: known stationary/no-ramp control.
    trends: tuple[Span, ...] | None = None
    evaluation_seconds: float = 60.0


def _points(samples):
    return tuple((float(t), float(v)) for t, v in samples)


def legacy_scenarios():
    yield TrendScenario('gc-flat', 'GC: repeated saws, no drift', 'GC: две пилы без дрейфа',
                        _points(gc_saw()), TrendConfig(1800, 100, 0.8), 'tests/scenarios.py: gc_saw',
                        'ед.', 'Нет тревог на этом стационарном fixture.', 'no_alarm', trends=())
    growth = Span(0, 5401, 'Дрейф +1.5 ед./мин', 'up')
    yield TrendScenario('gc-drift', 'GC: repeated saws + drift', 'GC: те же пилы и дрейф',
                        _points(gc_saw(drift=1.5)), TrendConfig(1800, 100, 0.8),
                        'tests/scenarios.py: gc_saw(drift=1.5)', 'ед.',
                        'Рост после прогрева; наклон около +1.5 ед./мин.', 'up',
                        events=(growth,), trends=(growth,))
    ramp = Span(300, 900, 'Рост CPU 20 → 80%', 'up')
    yield TrendScenario('cpu', 'CPU: +60 percentage points / 10 min', 'CPU: рост и новое плато',
                        _points(cpu_load()), TrendConfig(600, 160, 4), 'tests/scenarios.py: cpu_load',
                        'п.п. CPU', 'Обнаружить рост и перестать реагировать на установившееся плато.',
                        'up', events=(ramp,), trends=(ramp,))
    labels = {'quiet_rise': 'Плавный рост времени отклика', 'spike': 'Короткий всплеск времени отклика',
              'temporary_step': 'Временная ступенька', 'step': 'Постоянная ступенька',
              'backpressure': 'Backpressure: рост и восстановление'}
    for kind in ('quiet_rise', 'spike', 'temporary_step', 'step', 'backpressure'):
        events, trends, criterion = (), (), 'no_alarm'
        expectation = 'Всплеск или смена уровня не должны становиться трендовой тревогой.'
        if kind == 'quiet_rise':
            events = trends = (Span(240, 1201, 'Рост +2 мс/мин', 'up'),)
            criterion, expectation = 'up', 'Обнаружить рост не позднее двух минут после его начала.'
        elif kind == 'backpressure':
            trends = (Span(240, 480, 'Рост', 'up'), Span(480, 720, 'Восстановление', 'down'))
            events, criterion = trends, 'both'
            expectation = 'Обнаружить оба направления; после восстановления тревоги прекращаются.'
        elif kind == 'spike':
            events = (Span(360, 390, 'Всплеск двух отсчётов'),)
        elif kind == 'temporary_step':
            events = (Span(360, 600, 'Временный новый уровень'),)
        elif kind == 'step':
            events = (Span(360, 1201, 'Новый уровень'),)
        yield TrendScenario('response-' + kind, 'Response time: ' + kind, labels[kind],
                            _points(response(kind)), TrendConfig(120, 1, 1),
                            'tests/scenarios.py: response(' + kind + ')', 'мс', expectation,
                            criterion, events, trends)
    for suffix, old_name, label, cfg, criterion in (
        ('sensitive', 'sensitive (false positives expected)', 'Чувствительная настройка: ложные тревоги',
         TrendConfig(120, 10, 0.005), 'false_alarms'),
        ('tangent', 'tangent guard', 'Тот же шум: защита порогом наклона',
         TrendConfig(120, 10, 2), 'no_alarm'),
        ('variance', 'variance guard', 'Тот же шум: защита остаточной дисперсией',
         TrendConfig(120, 0.0001, 0.005), 'no_alarm'),
        ('long', 'long window', 'Тот же шум: длинное окно',
         TrendConfig(1800, 10, 0.03), 'no_alarm'),
    ):
        expectation = ('Намеренно показать ложные тревоги на стационарном шуме.' if criterion == 'false_alarms'
                       else 'На том же fixture выбранный защитный параметр подавляет тревоги.')
        yield TrendScenario('dirty-' + suffix, 'Dirty stationary latency: ' + old_name, label,
                            _points(dirty_latency()), cfg, 'tests/scenarios.py: dirty_latency(seed=404)',
                            'мс', expectation, criterion, trends=())


def bond_scenarios():
    from tests.prediction_interval_scenarios import previous_report_scenarios, stability_scenarios
    configs = {
        'correlation-trend-10-degrees': TrendConfig(300, 2, 0.4),
        'correlation-burst-20-percent': TrendConfig(120, 1, 1),
        'acf-white_noise': TrendConfig(120, 0.01, 0.1),
        'acf-variance_only': TrendConfig(300, 0.1, 0.2),
        'acf-periodic_unchanged': TrendConfig(480, 2, 0.3),
        'acf-periodic_new': TrendConfig(480, 2, 0.3),
        'acf-cycle_slower': TrendConfig(480, 2, 0.3),
        'acf-cycle_gradual': TrendConfig(480, 2, 0.3),
        'acf-ar_unchanged': TrendConfig(600, 10, 1),
        'acf-plateau_400': TrendConfig(120, 0.1, 0.5),
        'acf-recovery_8': TrendConfig(120, 0.1, 0.5),
        'cusum-quiet-saw': TrendConfig(120, 25, 2),
        'cusum-noisy-saw': TrendConfig(120, 25, 2),
        'cusum-rps-15s': TrendConfig(600, 400, 20),
    }
    for case in previous_report_scenarios():
        if case.name not in configs:
            continue
        points = _points((i*15, value) for i, value in enumerate(case.values))
        events = ((Span(case.start*15, case.end*15, 'Изменённый участок исходного fixture'),)
                  if case.start is not None and case.name not in ('acf-white_noise', 'acf-ar_unchanged',
                                                                 'acf-periodic_unchanged') else ())
        trends = None
        expectation = ('Наблюдательный пример: локальный наклон может отличаться от характера всего ряда. '
                       'Все реакции и причины отказа показаны без подгонки параметров.')
        if case.name in ('acf-white_noise', 'acf-variance_only', 'acf-ar_unchanged'):
            trends = ()
            expectation = 'Нет заданного монотонного тренда; реакции на шум видны как ложные тревоги этого контроля.'
        elif case.name == 'correlation-trend-10-degrees':
            trends = (Span(case.start*15, len(points)*15, 'Рост tan(10°) на отсчёт ≈ 0.7053 ед./мин', 'up'),)
            expectation = 'Обнаружить заданный рост после точки 100; угол задан в осях индекс/значение.'
        elif case.name == 'cusum-rps-15s':
            expectation = ('5171 значение реконструкции скриншота, не сырая телеметрия. '
                           'Эталон трендов неизвестен; показаны наблюдаемые реакции.')
        unit = 'RPS (реконструкция)' if case.name == 'cusum-rps-15s' else 'ед.'
        yield TrendScenario(case.name, case.label, 'Bond: ' + case.label, points, configs[case.name],
                            'tests/prediction_interval_scenarios.py → ' + case.name, unit,
                            expectation, events=events, trends=trends)
    for case in stability_scenarios(43):
        yield TrendScenario(case.name, case.label, 'Bond: ' + case.label,
                            _points((i*15, value) for i, value in enumerate(case.values)),
                            TrendConfig(480, 0.01, 0.02),
                            'tests/prediction_interval_scenarios.py: stability_scenarios(seed=43)',
                            'ед.', 'Высокий средний уровень и слабый цикл; нет заданного дрейфа.', trends=())


def report_scenarios():
    return [*legacy_scenarios(), *bond_scenarios()]
