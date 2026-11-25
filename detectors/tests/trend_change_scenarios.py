"""Reproducible change-point fixtures; old reports are observations, not labels."""

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Change:
    start: int
    direction: int
    end: int


@dataclass(frozen=True)
class Scenario:
    name: str
    label: str
    values: np.ndarray
    changes: tuple[Change, ...] | None = ()
    config: dict = field(default_factory=dict)
    group: str = 'control'
    source: str = 'tests/trend_change_scenarios.py'


def core_scenarios(seed=43):
    rng = np.random.default_rng(seed)
    n, start = 1000, 400
    t = np.arange(n, dtype=float)
    bend = np.maximum(0, t-start)
    yield Scenario('constant', 'Постоянный уровень', np.full(n, 100.))
    yield Scenario('steady-up', 'Постоянный рост: смены нет', 100+t*.2)
    yield Scenario('steady-down', 'Постоянное падение: смены нет', 100-t*.2)
    for name, base, delta in [('flat-up', 0, .2), ('flat-down', 0, -.2),
                               ('up-down', .2, -.4), ('down-up', -.2, .4),
                               ('up-flat', .2, -.2), ('down-flat', -.2, .2),
                               ('accelerate', .1, .3), ('slowdown', .4, -.3)]:
        for noise in (0., .2, 1.):
            y = 100+base*t+delta*bend+rng.normal(0, noise, n)
            yield Scenario(f'{name}-noise{noise:g}', f'{name}: шум σ={noise:g}', y,
                           (Change(start, int(np.sign(delta)), n),), group='change')
    for amplitude, period in ((1, 24), (2, 60), (2, 160)):
        background = amplitude*np.sin(t*2*np.pi/period)+rng.normal(0,.15,n)
        yield Scenario(f'cycle-trend-{period}', f'Цикл P={period} и новый рост',
                       100+background+.3*bend, (Change(start,1,n),), group='change-cycle')
    for sigma in (.1, 1, 5):
        yield Scenario(f'white-{sigma}', f'Белый шум σ={sigma}', 100+rng.normal(0,sigma,n))
    for rho in (.5, .9, .98):
        y = np.zeros(n)
        z = rng.normal(0,1,n)
        for i in range(1,n):
            y[i] = rho*y[i-1]+z[i]
        yield Scenario(f'ar-{rho}', f'AR({rho}): шум с памятью', 100+y, group='stress')
    yield Scenario('random-walk', 'Случайное блуждание: нет заданного излома',
                   100+np.cumsum(rng.normal(0,1,n)), group='stress')
    for period in (12,24,40,80,160,320,600,1200):
        for phase in (0.,1.3):
            yield Scenario(f'sine-{period}-{phase}', f'Синус P={period}, фаза={phase}',
                           100+3*np.sin(t*2*np.pi/period+phase), group='cycle')
    for period in (20,60,160):
        yield Scenario(f'saw-{period}', f'Пила P={period}', 100+(t%period)/period*6, group='cycle')
        yield Scenario(f'triangle-{period}', f'Треугольник P={period}',
                       100+np.abs((t%period)/period-.5)*6, group='cycle')
    yield Scenario('chirp', 'Цикл с меняющимся периодом',
                   100+3*np.sin(.02*t+.0001*t*t), group='cycle')
    yield Scenario('amplitude-change', 'Увеличение амплитуды цикла',
                   100+np.where(t<400,1,5)*np.sin(t*2*np.pi/40), group='stress')
    for width in (1,2,5,15,40,600):
        y = 100+rng.normal(0,.1,n)+20*((t>=400)&(t<400+width))
        yield Scenario(f'level-{width}', f'Новый уровень / импульс: {width} отсчётов', y)
    yield Scenario('variance-step', 'Дисперсия σ=0.1 → 5',
                   100+rng.normal(0,np.where(t<400,.1,5),n))
    yield Scenario('heavy-tail', 'Тяжёлый хвост Student t(3)', 100+rng.standard_t(3,n))
    y = 100+rng.normal(0,.1,n)
    y[200::97] += rng.choice([-100,100],len(y[200::97]))
    yield Scenario('repeated-spikes', 'Редкие разнонаправленные выбросы', y)
    for width in (4,8,12):
        yield Scenario(f'temporary-ramp-{width}', f'Краткая рампа {width} отсчётов',
                       100+.4*np.minimum(bend,width), group='stress')
    t2=np.arange(1900,dtype=float)
    y=100+.15*np.maximum(t2-360,0)-.35*np.maximum(t2-720,0)+.2*np.maximum(t2-1080,0)+.3*np.maximum(t2-1440,0)
    yield Scenario('piecewise', 'Четыре излома: рост → падение → плато → рост',
                   y+rng.normal(0,.15,len(y)), tuple(Change(s,d,e) for s,d,e in
                   [(360,1,720),(720,-1,1080),(1080,1,1440),(1440,1,1900)]), group='piecewise')
    y=100+.3*np.maximum(t-400,0)-.6*np.maximum(t-455,0)
    yield Scenario('close-bends', 'Близкие изломы: расстояние меньше большого окна',
                   y+rng.normal(0,.1,n),(Change(400,1,455),Change(455,-1,n)),group='stress-change')
    yield Scenario('stair-growth', 'Рост ступенями по 8 отсчётов',
                   100+.3*8*np.floor(bend/8)+rng.normal(0,.1,n),
                   (Change(400,1,n),), group='stress-change')
    yield Scenario('gradual', 'Плавное изменение наклона на протяжении 100 точек',
                   100+.0015*np.minimum(bend,100)**2+.3*np.maximum(bend-100,0),
                   (Change(400,1,n),), group='stress-change')
    yield Scenario('large-level', 'Уровень 1e12: малый наклон и шум',
                   1e12+.2*bend+rng.normal(0,.02,n), (Change(400,1,n),),group='numeric')
    for factor in (1e-6,1e6):
        yield Scenario(f'units-{factor}', f'Смена единиц ×{factor:g}',
                       factor*(100+.2*bend), (Change(400,1,n),),
                       config={'value_scale':factor},group='numeric')
    yield Scenario('warmup-change', 'Смена до окончания прогрева',
                   100+.2*np.maximum(t-50,0),(Change(50,1,n),),group='stress-change')
    yield Scenario('no-warmup', 'Всего 30 наблюдений', np.arange(30,dtype=float))


def inherited_scenarios():
    from tests.prediction_interval_scenarios import previous_report_scenarios, stability_scenarios
    for case in [*previous_report_scenarios(), *stability_scenarios(43)]:
        yield Scenario('previous-'+case.name, case.label, np.asarray(case.values),
                       changes=None, group='previous', source=case.source)


def monte_carlo_scenarios(seeds=20):
    """Fixed grid, no per-fixture tuning. Seeds 1000+ reserved for reporting."""
    n=800
    t=np.arange(n,dtype=float)
    for seed in range(1000,1000+seeds):
        rng=np.random.default_rng(seed)
        for noise in (.1,.5,1.,2.):
            z=rng.normal(0,noise,n)
            for delta in (-.3,-.1,.1,.3):
                yield Scenario(f'mc-{seed}-{noise}-{delta}', 'Monte Carlo: излом',
                               100+z+delta*np.maximum(t-320,0), (Change(320,int(np.sign(delta)),n),),
                               group=f'mc-change-noise{noise:g}-delta{abs(delta):g}')
        for name,y in [('white',rng.normal(0,1,n)),
                       ('cycle',3*np.sin(t*2*np.pi/60+rng.uniform(0,6))),
                       ('long-sine',3*np.sin(t*2*np.pi/600+rng.uniform(0,6))),
                       ('long-triangle',6*np.abs(((t+rng.uniform(0,600))%600)/600-.5)),
                       ('step',(t>=320)*20+rng.normal(0,.1,n)),
                       ('walk',np.cumsum(rng.normal(0,1,n))),
                       ('ar09', np.zeros(n)),('ar098',np.zeros(n))]:
            if name in ('ar09','ar098'):
                rho=.9 if name=='ar09' else .98
                for i in range(1,n):
                    y[i]=rho*y[i-1]+rng.normal()
            yield Scenario(f'mc-{seed}-{name}', f'Monte Carlo: {name}',100+y,group='mc-control-'+name)
