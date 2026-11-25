"""Large causal TrendDetector report: onset errors, controls, MC, old fixtures, speed."""

import argparse
import csv
from dataclasses import asdict, replace
import hashlib
import html
import json
import math
from pathlib import Path
import platform
import re
import shutil
import statistics
import time

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from apps.detectors import TrendDetector
from tests.trend_change_scenarios import core_scenarios, inherited_scenarios, monte_carlo_scenarios
from tests.trend_replay_appendices import appendices_html, create_appendices


def save_csv(path, rows):
    fields=list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def replay(case):
    detector=TrendDetector(**case.config)
    rows=[]
    for i,value in enumerate(case.values):
        result=detector.update(float(value))
        rows.append({'index':i,'value':float(value),**asdict(result)})
    return rows


def summarize(case, rows, stem):
    events={r['confirmed_at']:r for r in rows if r['detected']}
    # An event is keyed by its first online confirmation, not each alarm sample.
    events=[events[k] for k in sorted(events)]
    reactions=[]
    matched=set()
    if case.changes is not None:
        for change in case.changes:
            candidates=[r for r in events if change.start<=r['confirmed_at']<change.end
                        and r['direction']==change.direction and r['confirmed_at'] not in matched]
            first=candidates[0] if candidates else None
            if first:
                matched.add(first['confirmed_at'])
            reactions.append({**asdict(change),'detected':first is not None,
                              'confirmed_at':first['confirmed_at'] if first else None,
                              'estimated_start':first['change_start'] if first else None,
                              'delay':first['confirmed_at']-change.start if first else None,
                              'onset_error':first['change_start']-change.start if first else None})
    ready=[r for r in rows if r['ready']]
    return {'name':case.name,'label':case.label,'group':case.group,'source':case.source,
            'stem':stem,'points':len(rows),'config':case.config,
            'data_sha256':hashlib.sha256(np.asarray(case.values,dtype='<f8').tobytes()).hexdigest(),
            'ready_samples':len(ready),'alarm_samples':sum(r['detected'] for r in rows),
            'confirmed_events':len(events),'unmatched_events':len(events)-len(matched) if case.changes is not None else None,
            'labelled_changes':len(case.changes) if case.changes is not None else None,
            'detected_changes':sum(r['detected'] for r in reactions) if case.changes is not None else None,
            'max_score':max((r['score'] for r in rows),default=0),
            'horizontal_fraction_ready':sum(r['horizontal'] for r in ready)/len(ready) if ready else None,
            'first_confirmation':events[0]['confirmed_at'] if events else None,
            'reactions':reactions,'events':[{'confirmed_at':r['confirmed_at'],'estimated_start':r['change_start'],
                                          'direction':r['direction']} for r in events]}


def aggregate(infos):
    result=[]
    for group in dict.fromkeys(r['group'] for r in infos):
        cases=[r for r in infos if r['group']==group]
        reactions=[v for r in cases for v in r['reactions']]
        found=[v for v in reactions if v['detected']]
        errors=[abs(v['onset_error']) for v in found]
        delays=[v['delay'] for v in found]
        controls=[r for r in cases if r['labelled_changes']==0]
        known=any(r['labelled_changes'] is not None for r in cases)
        result.append({'group':group,'cases':len(cases),'labelled_changes':len(reactions) if known else None,
                       'detected_changes':len(found) if known else None,'recall':len(found)/len(reactions) if reactions else None,
                       'controls':len(controls),'controls_with_alarm':sum(r['confirmed_events']>0 for r in controls),
                       'unmatched_events':sum(r['unmatched_events'] or 0 for r in cases) if known else None,
                       'onset_mae':statistics.mean(errors) if errors else None,
                       'onset_abs_p95':float(np.percentile(errors,95)) if errors else None,
                       'delay_median':statistics.median(delays) if delays else None,
                       'delay_p95':float(np.percentile(delays,95)) if delays else None})
    return result


def render_case(output, case, rows, info):
    x=np.arange(len(rows))
    score=np.array([r['score'] for r in rows])
    selected=info['first_confirmation']
    if selected is None:
        selected=int(np.argmax(score)) if max(score)>0 else len(rows)-1
    cfg=TrendDetector(**case.config)
    fig,axes=plt.subplots(4,1,figsize=(13,10),sharex=True,layout='constrained',
                          gridspec_kw={'height_ratios':[3,1.6,1.6,1.6]})
    fig.suptitle(case.label,fontsize=13)
    axes[0].plot(x,case.values,color='#334155',lw=.8,label='Исходный ряд')
    a=max(0,selected-cfg.data_max_size+1)
    b=max(a,selected-cfg.window+1)
    axes[0].axvspan(a,b,color='#7c3aed',alpha=.12,label='Большое опорное окно')
    axes[0].axvspan(b,selected,color='#f59e0b',alpha=.2,label='Концевое окно')
    if rows[selected]['ready']:
        y=np.asarray(case.values[a:b])
        center=(a+b-1)*.5
        # Public reference slope + actual reference mean reconstructs the fitted line.
        slope=rows[selected]['reference_slope']*cfg.sample_interval/cfg.time_scale*cfg.value_scale
        axes[0].plot([a,selected],[np.mean(y)+slope*(j-center) for j in (a,selected)],
                     color='#7c3aed',lw=1.3,ls='--',label='Продолжение опорной линии')
    for event in info['events']:
        axes[0].axvline(event['confirmed_at'],color='#dc2626',lw=1,ls='--')
        axes[0].axvline(event['estimated_start'],color='#0891b2',lw=1)
    ready=np.array([r['ready'] for r in rows])
    axes[1].plot(x,[r['slope'] if r['ready'] else np.nan for r in rows],label='Наклон конца')
    axes[1].plot(x,[r['reference_slope'] if r['ready'] else np.nan for r in rows],label='Наклон опоры',alpha=.8)
    bound=math.tan(math.radians(cfg.angle_tolerance))
    axes[1].axhspan(-bound,bound,color='#16a34a',alpha=.13,label=f'Горизонталь ±{cfg.angle_tolerance:g}°')
    axes[1].set_ylabel('Норм. ед./ед. времени')
    axes[2].plot(x,score,color='#dc2626',label='Накопленный score')
    axes[2].plot(x,[r['evidence'] for r in rows],color='#94a3b8',lw=.7,label='Текущее evidence')
    axes[2].axhline(.8,color='#991b1b',ls='--',lw=.8,label='Порог подтверждения 0.8')
    axes[2].set_ylim(-.03,1.04)
    axes[2].set_ylabel('0…1')
    estimates=[r['change_start'] if r['detected'] else np.nan for r in rows]
    axes[3].plot(x,estimates,'.',ms=2,color='#0891b2',label='Оценённое начало (доступно после подтверждения)')
    if case.changes is not None:
        for event in case.changes:
            for ax in axes[:3]:
                ax.axvline(event.start,color='#16a34a',lw=.8,ls=':')
            axes[3].plot([event.start,event.end],[event.start,event.start],color='#16a34a',lw=.8,ls=':')
    axes[3].set_ylabel('Индекс начала')
    axes[3].set_xlabel('Индекс отсчёта; одинаковый шаг, единицы задаются параметрами')
    for ax in axes:
        ax.grid(alpha=.15)
        ax.legend(loc='upper left',fontsize=7,ncol=2)
    if not np.any(ready):
        axes[0].text(.5,.5,'ПРОГРЕВ НЕ ЗАВЕРШЁН',transform=axes[0].transAxes,ha='center')
    fig.savefig(output/f'{info["stem"]}.png',dpi=105)
    plt.close(fig)


def benchmark(samples=60000):
    t=np.arange(samples,dtype=float)
    signals={'stationary':np.sin(t*.11),
             'piecewise':np.cumsum(np.where((t.astype(int)//300)%2, .2,-.2))}
    result=[]
    for size in (240,1200,4800):
        # Include regimes longer than the baseline in every configuration.
        signals['piecewise']=np.cumsum(np.where((t.astype(int)//(size+80))%2,.2,-.2))
        for name,values in signals.items():
            elapsed=[]
            for _ in range(3):
                d=TrendDetector(data_max_size=size,window=40)
                start=time.perf_counter()
                for value in values:
                    d.update(float(value))
                elapsed.append(time.perf_counter()-start)
            seconds=statistics.median(elapsed)
            result.append({'data_max_size':size,'window':40,'signal':name,'samples':samples,
                           'median_seconds':seconds,'updates_per_second':samples/seconds,
                           'microseconds_per_update':seconds/samples*1e6,
                           'repeats':3,'stored_values':len(d._tail.values)+len(d._reference.values)})
    return result


def sensitivity_sweep():
    cases=[*core_scenarios(),*monte_carlo_scenarios(6)]
    result=[]
    for window,size,threshold in ((20,120,2),(40,240,2),(60,360,2),(40,240,1.5),(40,240,3)):
        config={'window':window,'data_max_size':size,'threshold':threshold}
        infos=[]
        for c in cases:
            case=replace(c,config={**c.config,**config})
            infos.append(summarize(case,replay(case),''))
        groups=aggregate(infos)
        found=[e for r in infos for e in r['reactions'] if e['detected']]
        truth=sum(r['labelled_changes'] or 0 for r in infos)
        controls=[r for r in infos if r['labelled_changes']==0]
        result.append({**config,'cases':len(cases),'labelled_changes':truth,'detected_changes':len(found),
                       'controls_with_alarm':sum(r['confirmed_events']>0 for r in controls),
                       'controls':len(controls),'onset_mae':statistics.mean(abs(e['onset_error']) for e in found) if found else None,
                       'delay_median':statistics.median(e['delay'] for e in found) if found else None,
                       'groups':groups})
    return result


def load_test_log(path):
    if path is None:
        return None
    contents=path.read_text(encoding='utf-8')
    match=re.search(r'Ran (\d+) tests? in ([\d.]+)s',contents)
    if not match or not re.search(r'^OK(?: \(skipped=\d+\))?\s*$',contents,re.M) or 'FAILED (' in contents:
        raise ValueError('test log must be a completed successful unittest run')
    skip=re.search(r'OK \(skipped=(\d+)\)',contents)
    return {'tests':int(match[1]),'seconds':float(match[2]),'skipped':int(skip[1]) if skip else 0}


def render_overview(output, report):
    infos=report['scenarios']
    reactions=[e for r in infos if r['group']!='previous' for e in r['reactions'] if e['detected']]
    fig,axes=plt.subplots(2,2,figsize=(13,9),layout='constrained')
    axes[0,0].hist([r['onset_error'] for r in reactions],bins=35,color='#0891b2')
    axes[0,0].set_title('Ошибка оценки начала: estimate − truth')
    axes[0,1].hist([r['delay'] for r in reactions],bins=30,color='#dc2626')
    axes[0,1].set_title('Задержка онлайн-подтверждения')
    groups=[g for g in report['groups'] if g['group'].startswith('mc-change')]
    axes[1,0].barh([g['group'].replace('mc-change-','') for g in groups],[g['recall'] for g in groups],color='#2563eb')
    axes[1,0].set_xlim(0,1.05)
    axes[1,0].set_title('Доля обнаруженных смен: Monte Carlo')
    groups=[g for g in report['groups'] if g['controls']]
    axes[1,1].barh([g['group'] for g in groups],[g['controls_with_alarm']/g['controls'] for g in groups],color='#d97706')
    axes[1,1].set_xlim(0,1.05)
    axes[1,1].set_title('Доля контрольных рядов с ложным событием')
    for ax in axes.flat:
        ax.grid(alpha=.15)
    fig.savefig(output/'overview.png',dpi=120)
    plt.close(fig)


def render_html(output, report):
    def num(v):
        return '—' if v is None else f'{v:.3g}'
    by_group={g['group']:g for g in report['groups']}
    highlights=[]
    main=by_group.get('change')
    if main:
        highlights.append(f'Основные смены: {main["detected_changes"]}/{main["labelled_changes"]}; '
                          f'MAE начала {num(main["onset_mae"])} отсчёта; медиана задержки {num(main["delay_median"])}.')
    mc=[g for g in report['groups'] if g['group'].startswith('mc-change')]
    if mc:
        highlights.append(f'Monte Carlo: {sum(g["detected_changes"] for g in mc)}/{sum(g["labelled_changes"] for g in mc)} смен. '
                          'Слабые эффекты на сильном шуме часто пропускаются; показатели по SNR раскрыты ниже.')
    triangle=by_group.get('mc-control-long-triangle')
    if triangle:
        highlights.append(f'Длинный треугольный цикл P=600 при окне 240: ложные события в '
                          f'{triangle["controls_with_alarm"]}/{triangle["controls"]} рядов. Для таких циклов нужен более длинный контекст.')
    highlights=''.join(f'<li>{v}</li>' for v in highlights)
    appendix_nav=''
    if report.get('appendices'):
        appendix_nav='<p>Дополнения в конце: <a href="#screenshot-15s">исправленный график со скриншота, шаг 15 с</a>'
        if 'influx' in report['appendices']:
            appendix_nav+=' · <a href="#influx-cassandra">четыре ряда из InfluxDB</a>'
        appendix_nav+='.</p>'
        check=report.get('appendix_test_run')
        if check:
            appendix_nav+=(f'<p>Отдельная проверка дополнений и детектора: {check["tests"]} тестов, '
                           f'пропущено {check["skipped"]}. <a href="appendix-test-results.txt">Журнал дополнений</a>.</p>')
    groups=''.join(f'<tr><td>{html.escape(g["group"])}</td><td>{g["cases"]}</td>'
                   f'<td>{num(g["detected_changes"])}/{num(g["labelled_changes"])}</td><td>{num(g["recall"])}</td>'
                   f'<td>{g["controls_with_alarm"]}/{g["controls"]}</td><td>{num(g["unmatched_events"])}</td>'
                   f'<td>{num(g["onset_mae"])}</td><td>{num(g["onset_abs_p95"])}</td>'
                   f'<td>{num(g["delay_median"])}/{num(g["delay_p95"])}</td></tr>' for g in report['groups'])
    table=[]
    details=[]
    for r in report['scenarios']:
        plotted=r['plotted']
        label=html.escape(r['label'])
        title=f'<a href="#{r["stem"]}">{label}</a>' if plotted else label
        table.append(f'<tr><td>{r["stem"]}. {title}</td><td>{r["points"]}</td><td>{num(r["max_score"])}</td>'
                     f'<td>{r["confirmed_events"]}</td><td>{num(r["detected_changes"])}/{num(r["labelled_changes"])}</td>'
                     f'<td>{num(r["unmatched_events"])}</td><td><a href="{r["stem"]}-points.csv">CSV</a></td></tr>')
        if not plotted:
            continue
        events=''.join(f'<tr><td>{e["start"]}</td><td>{e["direction"]:+d}</td><td>{num(e["confirmed_at"])}</td>'
                       f'<td>{num(e["estimated_start"])}</td><td>{num(e["delay"])}</td><td>{num(e["onset_error"])}</td></tr>'
                       for e in r['reactions'])
        label_text=('Эталон смен не задан: наблюдательный ряд из прежнего отчёта.' if r['labelled_changes'] is None
                    else f'Размечено смен: {r["labelled_changes"]}; обнаружено: {r["detected_changes"]}; '
                    f'несопоставленных подтверждений: {r["unmatched_events"]}.')
        details.append(f'<section id="{r["stem"]}"><h2>{r["stem"]}. {label}</h2><p>{label_text}</p>'
                       f'<p>Источник: {html.escape(r["source"])}. Конфигурация поверх defaults: '
                       f'<code>{html.escape(json.dumps(r["config"],ensure_ascii=False))}</code>. '
                       f'Готовых точек: {r["ready_samples"]}/{r["points"]}; '
                       f'horizontal среди готовых: {num(r["horizontal_fraction_ready"])}.</p>'
                       f'<img src="{r["stem"]}.png" loading="lazy" alt="{label}">'
                       f'<table><tr><th>Истинное начало</th><th>Δнаклон</th><th>Подтверждено</th><th>Оценка начала</th>'
                       f'<th>Задержка</th><th>Ошибка начала</th></tr>{events}</table></section>')
    benches=''.join(f'<tr><td>{b["data_max_size"]}/40</td><td>{b["signal"]}</td>'
                    f'<td>{b["updates_per_second"]:,.0f}</td><td>{b["microseconds_per_update"]:.2f}</td></tr>' for b in report['benchmark'])
    sweep=''.join(f'<tr><td>{s["window"]}/{s["data_max_size"]}</td><td>{s["threshold"]}</td>'
                  f'<td>{s["detected_changes"]}/{s["labelled_changes"]}</td>'
                  f'<td>{s["controls_with_alarm"]}/{s["controls"]}</td><td>{num(s["onset_mae"])}</td>'
                  f'<td>{num(s["delay_median"])}</td></tr>' for s in report['sensitivity'])
    test=report['test_run']
    test_text=(f'<p>Проверки: {test["tests"]} тестов, пропущено {test["skipped"]}; '
               '<a href="test-results.txt">журнал</a>.</p>' if test else '<p>Журнал unittest не приложен.</p>')
    page=f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>TrendDetector: смена тренда и момент начала</title><style>
body{{font:15px/1.55 system-ui;margin:28px auto;max-width:1450px;padding:0 20px;color:#243247}}
h1,h2{{line-height:1.2}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{padding:7px;border-bottom:1px solid #ddd;text-align:left}}
th{{background:#edf2f7;position:sticky;top:0}}img{{width:100%;height:auto}}section{{margin-top:48px;border-top:1px solid #ccd5df;padding-top:15px}}
.scroll{{overflow:auto;max-height:650px}}code,pre{{background:#eef2f6}}pre{{padding:16px;overflow:auto}}a{{color:#145dc5}}</style>
<h1>TrendDetector: устойчивая смена тренда и оценка её начала</h1>
<p>3 ноября 2026. {len(report['scenarios'])} рядов; {report['total_points']:,} отсчётов. Синтетические данные и
неизменённые fixtures прежних отчётов. Единая конфигурация 40/240, память 13, threshold=2; исключение — явно заданная смена единиц.</p>
{test_text}<p><a href="summary.json">JSON с параметрами и SHA256</a> · <a href="summary.csv">Сводка CSV</a> ·
<a href="events.csv">Все истинные смены и ошибки CSV</a> · <a href="groups.csv">Группы CSV</a> · <a href="benchmark.csv">Производительность CSV</a></p>
<h2>Главные результаты</h2><ul>{highlights}</ul>
{appendix_nav}
<h2>Как устроен детектор</h2><p>Большое окно 240 включает опорные 200 точек и концевые 40. Опорная линия описывает
предыдущий режим; концевое окно измеряет новый наклон и смещение относительно её продолжения. Учитываются оба признака.
Знаменатель — максимум остаточных масштабов sqrt(SSE/(n−2)) двух линий и noise_floor; он не уменьшается на √n, чтобы цикл или шум с памятью
не превращались автоматически в множество независимых подтверждений.</p>
<pre>strength = min(|b_tail − b_ref| × (tail−1), |tail_mean − reference_prediction|) / (threshold × RMS)
evidence = clip(strength − 1, 0, 1), если знаки смещения и изменения наклона совпадают
score = среднее evidence за последние inertia_window точек одного направления
score ≥ 0.8 → проверка непрерывного излома против прямой и двух независимых линий
проверка не пройдена → score ограничен 0.79; пройдена → разрешено накопление до 1</pre>
<p>При смене направления короткая память очищается; при исчезновении evidence она затухает за 13 точек.
Score — инженерная сила подтверждения, не вероятность. Подтверждение означает detected=True (≥0.8); единица означает полную
короткую память сильных подтверждений. После адаптации большого окна score снижается даже на продолжающемся новом тренде.</p>
<p>Излом ищется по всем допустимым позициям уже наблюдённого большого окна. Непрерывная ломаная должна уменьшить SSE
одной линии минимум вдвое, а её SSE не должна превосходить SSE двух независимых линий больше чем в 1.3 раза.
Дополнительно SSE излома должна быть ≤0.8 SSE гладкого кубического полинома, чтобы отвергать плавные волны. Так отвергаются ступеньки и многие выбросы. Это эвристика; не статистический тест с заданной ошибкой первого рода.</p>
<h2>Начало и подтверждение — разные времена</h2><p>Зелёный пунктир — истинное начало, красный штрих — онлайн-подтверждение,
голубая линия — оценка начала, ставшая доступной в момент красного штриха. Оценка начала фиксируется при первом подтверждении
эпизода. Будущие значения не используются. Ошибка начала = estimate − truth; задержка = confirmed_at − truth.
Обе метрики в отсчётах. Для секунд умножить на sample_interval. Излом сопоставляется только подтверждению в его интервале
и с правильным направлением изменения наклона; одно подтверждение сопоставляется одной смене. Остальные подтверждения
несопоставлены, пропуски остаются в знаменателе recall. Несопоставленные события в размеченном контроле — ложные тревоги.</p>
<p>Индексы нулевые. В y=a+b*x+delta*max(0,x−k) точка k лежит на пересечении линий и считается началом.
Разрешён излом между отсчётами; возвращается ближайший целый индекс.
direction обозначает изменение наклона: замедление роста имеет −1. Несколько detected=True одного эпизода — одно событие,
определяемое confirmed_at. Горизонтальность оценивается по концевой линии: |atan(slope)| ≤ 1°.
slope=(значение/value_scale) за time_scale при шаге sample_interval. Угол зависит от осей. До прогрева horizontal=None.</p>
<h2>Результаты по группам</h2><p>Monte Carlo использует зарезервированные seed 1000…{999+report['mc_seeds']}.
Настройки не подбираются по отдельным рядам. Ошибки и задержки считаются только для обнаруженных смен;
recall показывает оставшиеся пропуски. P95 — эмпирический квантиль, не доверительный интервал.
Контроли AR и случайное блуждание размечены как «нет заданного излома»; их локальные дрейфы могут давать ложные события.</p>
<div class="scroll"><table><tr><th>Группа</th><th>Рядов</th><th>Найдено/смен</th><th>Recall</th>
<th>Контролей с тревогой/всего</th><th>Несопоставлено</th><th>MAE начала</th><th>P95 |ошибки|</th><th>Задержка median/P95</th></tr>{groups}</table></div>
<img src="overview.png" alt="Ошибки начала, задержки и контрольные тревоги">
<h2>Окна и чувствительность</h2><p>Одна и та же сетка основных сценариев и Monte Carlo на 6 seed;
каждая конфигурация применяется ко всем рядам. Более длинное концевое окно усредняет шум,
но увеличивает задержку и может пропускать короткие режимы. Threshold снижает либо повышает требования к эффекту.
Детальные группы: <a href="sensitivity.json">JSON</a>.</p>
<table><tr><th>Концевое/большое</th><th>Threshold</th><th>Найдено/смен</th><th>Контролей с тревогой</th><th>MAE начала</th><th>Задержка median</th></tr>{sweep}</table>
<h2>Производительность</h2><p>CPython {report['python']}; {html.escape(report['platform'])}.
Три повтора, медиана; по 60 000 обновлений, float-конверсия и создание результата включены, генерация данных и графики исключены.
Время импорта SciPy исключено. Один поток, один экземпляр на ряд. Измерение зависит от машины и нагрузки.</p>
<table><tr><th>Большое/концевое</th><th>Сигнал</th><th>Обновлений/с</th><th>мкс/обновление</th></tr>{benches}</table>
<p>Потоковые OLS-моменты обновляются за O(1) амортизированно с периодическим пересчётом и сменой локального начала координат.
Поиск излома O(W) выполняется только для нового кандидата, не чаще раза в длину короткой памяти.
Общая верхняя амортизированная оценка O(1+W/I), худшее обновление O(W), память O(W+I). Время не растёт с длиной всей истории.</p>
<h2>Ограничения, видимые в тестах</h2><p>Излом до прогрева может быть потерян; слабый новый наклон на сильном шуме может не накопить подтверждение.
Большой цикл, кусок которого длиннее окна, и нестационарный коррелированный шум могут выглядеть как смена тренда.
Два близких излома загрязняют опорную линию; плавное изменение наклона не имеет единственной геометрической точки излома,
поэтому его оценка относительно заданного начала может быть смещена. Ступенчатый рост может отвергаться как смена уровня.
Неравномерный шаг необходимо предварительно привести к регулярному; пропуски не интерполируются самим детектором.</p>
<p>У прежних fixtures нет достоверной разметки именно смены наклона: для них показаны все реакции без выдуманных recall и ошибок начала.
RPS со скриншота — реконструкция, не сырая телеметрия. Синтетические проверки не гарантируют отсутствие ложных тревог в эксплуатации.</p>
<h2>Все ряды</h2><p>Графики есть для основных и прежних сценариев. У каждого Monte Carlo ряда сохранён полный поточечный CSV.</p>
<div class="scroll"><table><tr><th>Сценарий</th><th>Точек</th><th>Max score</th><th>Событий</th><th>Найдено/смен</th><th>Несопоставлено</th><th>Данные</th></tr>{''.join(table)}</table></div>
{''.join(details)}<h2>Воспроизведение</h2><pre>New-Item -ItemType Directory -Force artifacts/trend-change | Out-Null
uv run python -m unittest discover -s tests -t . -v *> artifacts/trend-change/test-results.txt
uv run python -m tests.visualize_trend_change --output artifacts/trend-change --mc-seeds 40 --test-log artifacts/trend-change/test-results.txt</pre>
<p>SHA256 apps/detectors.py: <code>{report['detector_sha256']}</code>. Все конфигурации и хеши входов записаны в JSON.</p>
{appendices_html(report.get('appendices'))}</html>'''
    (output/'report.html').write_text(page,encoding='utf-8')


def create_report(output, *, scenarios=None, mc_seeds=40, test_log=None, with_benchmark=True, reference_image=None):
    output.mkdir(parents=True,exist_ok=True)
    test_run=load_test_log(test_log)
    if test_log and test_log.resolve()!=(output/'test-results.txt').resolve():
        shutil.copy2(test_log,output/'test-results.txt')
    cases=list(scenarios) if scenarios is not None else [*core_scenarios(),*inherited_scenarios(),*monte_carlo_scenarios(mc_seeds)]
    infos=[]
    for index,case in enumerate(cases,1):
        stem=f'{index:04d}'
        rows=replay(case)
        info=summarize(case,rows,stem)
        info['plotted']=not case.group.startswith('mc-')
        save_csv(output/f'{stem}-points.csv',rows)
        if info['plotted']:
            render_case(output,case,rows,info)
        infos.append(info)
    source=Path(__file__).parents[1]/'apps'/'detectors.py'
    defaults=TrendDetector()
    report={'schema_version':1,'date':'2026-11-03','python':platform.python_version(),
            'numpy':np.__version__,'matplotlib':matplotlib.__version__,'platform':platform.platform(),
            'detector_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'default_config':{k:getattr(defaults,k) for k in ('window','data_max_size','inertia_window','threshold',
                              'value_scale','sample_interval','time_scale','angle_tolerance','noise_floor')},
            'mc_seeds':mc_seeds,'scenario_count':len(infos),'total_points':sum(r['points'] for r in infos),
            'test_run':test_run,'groups':aggregate(infos),'scenarios':infos,
            'benchmark':benchmark() if with_benchmark else [],
            'sensitivity':sensitivity_sweep() if with_benchmark else []}
    if scenarios is None:
        report['appendices']=create_appendices(output,reference_image=reference_image)
        report['generator_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        report['appendix_generator_sha256']=hashlib.sha256(Path(__file__).with_name('trend_replay_appendices.py').read_bytes()).hexdigest()
    save_csv(output/'summary.csv',[{k:v for k,v in r.items() if k not in ('reactions','events','config')} for r in infos])
    save_csv(output/'groups.csv',report['groups'])
    save_csv(output/'events.csv',[{'name':r['name'],'group':r['group'],**e} for r in infos for e in r['reactions']])
    save_csv(output/'benchmark.csv',report['benchmark'])
    (output/'sensitivity.json').write_text(json.dumps(report['sensitivity'],ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    render_overview(output,report)
    render_html(output,report)
    return report


def append_replays(output, reference_image=None, test_log=None):
    """Add observations without recomputing or replacing the 1101 base fixtures."""
    report=json.loads((output/'summary.json').read_text(encoding='utf-8'))
    source=Path(__file__).parents[1]/'apps'/'detectors.py'
    if hashlib.sha256(source.read_bytes()).hexdigest()!=report['detector_sha256']:
        raise ValueError('Detector changed: regenerate the base report before appending observations')
    report['appendices']=create_appendices(output,reference_image=reference_image)
    report['generator_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['appendix_generator_sha256']=hashlib.sha256(Path(__file__).with_name('trend_replay_appendices.py').read_bytes()).hexdigest()
    report.pop('appendix_test_run',None)
    if test_log is not None:
        report['appendix_test_run']=load_test_log(test_log)
        if test_log.resolve()!=(output/'appendix-test-results.txt').resolve():
            shutil.copy2(test_log,output/'appendix-test-results.txt')
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    render_html(output,report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('artifacts/trend-change'))
    parser.add_argument('--mc-seeds',type=int,default=40)
    parser.add_argument('--test-log',type=Path)
    parser.add_argument('--append-replays',action='store_true',help='Append screenshot/InfluxDB observations to an existing report')
    parser.add_argument('--reference-image',type=Path)
    args=parser.parse_args()
    if args.mc_seeds<1:
        parser.error('--mc-seeds must be positive')
    if args.append_replays:
        result=append_replays(args.output,reference_image=args.reference_image,test_log=args.test_log)
        print(json.dumps(result['appendices'],ensure_ascii=False,indent=2))
    else:
        result=create_report(args.output,mc_seeds=args.mc_seeds,test_log=args.test_log,reference_image=args.reference_image)
        print(json.dumps({k:result[k] for k in ('scenario_count','total_points','groups','benchmark')},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
