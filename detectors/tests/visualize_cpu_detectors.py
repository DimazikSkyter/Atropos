"""Before/after replay of the exact 3h/15s CPU that exposed weak detector scores."""
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import html
import json
from pathlib import Path
import time

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from apps.detectors import CusumDetector, TrendDetector


def main():
    root=Path(__file__).resolve().parents[1]
    fixture=root/'tests/fixtures/cusum-cpu-20261122-3h-15s.json'
    source=json.loads(fixture.read_text(encoding='utf-8'))
    output=root/'artifacts/cpu-detectors'
    output.mkdir(parents=True,exist_ok=True)
    values=source['values']
    cusum,trend=CusumDetector(drift_mode='adaptive'),TrendDetector()
    rows=[]
    started=time.perf_counter()
    for i,value in enumerate(values):
        c=cusum.update(value)
        t=trend.update(value)
        rows.append(dict(time_ns=source['times'][i],value=value,cusum=c if cusum.ready else None,
            cusum_before=source['before']['CUSUM']['scores'][i],drift=cusum.drift_allowance,
            sigma=cusum.noise_std,trend=t.score if t.ready else None,
            trend_before=source['before']['Trend']['scores'][i],trend_detected=t.detected))
    elapsed=time.perf_counter()-started
    scores=np.array([r['cusum'] or 0. for r in rows])
    trend_scores=np.array([r['trend'] or 0. for r in rows])
    assert list(scores[:480]) == CusumDetector(drift_mode='adaptive').detect_single_seria(values[:480])
    assert list(trend_scores[:480]) == TrendDetector().detect_single_seria(values[:480])
    summary={'points':len(values),'step':source['step'],'threshold':cusum.threshold,
        'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'implementation_sha256':hashlib.sha256((root/'apps/detectors.py').read_bytes()).hexdigest(),
        'cusum_before_max':max(x or 0 for x in source['before']['CUSUM']['scores']),
        'cusum_after_max':float(scores.max()),'ordinary_cycle_mean':float(scores[100:200].mean()),
        'irregular_section_max':float(scores[200:320].max()),
        'trend_before_max':max(x or 0 for x in source['before']['Trend']['scores']),
        'trend_after_max':float(trend_scores.max()),'trend_earlier_max':float(trend_scores[:680].max()),
        'trend_confirmed_points':sum(r['trend_detected'] for r in rows),
        'prefix_causality':'PASS','replay_both_seconds':elapsed}
    (output/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    with (output/'points.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    tz=timezone(timedelta(hours=3))
    x=[datetime.fromtimestamp(t/1e9,tz) for t in source['times']]
    fig,axes=plt.subplots(3,1,sharex=True,figsize=(14,9))
    axes[0].plot(x,values,color='#2563eb',lw=1,label='Исходный CPU: 720 точек / 15 с')
    for axis,kind,color in ((axes[1],'cusum','#dc2626'),(axes[2],'trend','#16a34a')):
        axis.plot(x,[r[kind+'_before'] for r in rows],color='#94a3b8',ls='--',label='До исправления')
        axis.plot(x,[r[kind] for r in rows],color=color,label='После исправления')
        axis.set_ylim(-.02,1.02);axis.set_ylabel(kind.upper())
    for axis in axes:
        axis.grid(axis='y',color='#e2e8f0')
        axis.legend(loc='upper left',bbox_to_anchor=(0,-.17),ncol=2,frameon=False,fontsize=10)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=tz))
    axes[-1].set_xlabel('22 ноября 2026 · Europe/Moscow')
    fig.suptitle('Точный трёхчасовой CPU: реакции до и после исправления',x=.08,ha='left')
    fig.subplots_adjust(left=.08,right=.98,top=.93,bottom=.10,hspace=.55)
    fig.savefig(output/'comparison.png',dpi=130);plt.close(fig)
    text=f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>CPU: проверка CUSUM и Trend</title>
<style>body{{font:16px/1.5 system-ui;max-width:1100px;margin:32px auto;padding:20px;color:#172033}}img{{width:100%}}pre{{white-space:pre-wrap}}</style>
<h1>CPU: CUSUM и Trend до и после исправления</h1>
<p>Тот же трёхчасовой расчёт из PostgreSQL, 720 исходных точек с шагом 15 секунд. Данные не сглажены;
старые кривые взяты из сохранённого результата, новые рассчитаны текущим классом последовательно.</p>
<pre>{html.escape(source['query'])}</pre><img src="comparison.png" alt="Реакции на точном CPU">
<p>CUSUM: максимум {summary['cusum_before_max']:.6f} → {summary['cusum_after_max']:.6f}.
Среднее на обычных циклах: {summary['ordinary_cycle_mean']:.6f}; максимум на первом участке нерегулярных всплесков:
{summary['irregular_section_max']:.6f}. Threshold остался 5. Допуск — 50% фоновой sigma предыдущих дельт,
с минимальным относительным допуском. 95-й процентиль ограничивает редкие хвосты оценки sigma и больше не
отсекает почти все отдельные изменения.</p>
<p>Trend: максимум {summary['trend_before_max']:.6f} → {summary['trend_after_max']:.6f}; до последних 40 точек максимум
{summary['trend_earlier_max']:.6f}. Это предварительная реакция на рост: строгая проверка излома пока не подтверждена,
detected=True не выдаётся. Изменения наклона и смещения сравниваются с HAC-погрешностями регрессионных линий;
коррелированный шум не считается независимыми повторениями.</p>
<p>Причинность: оценки первых 480 точек совпадают при прогоне 480 и 720 точек. Будущая часть ряда не влияет
на прошлые оценки. Прогрев сохранён. Прогон обоих детекторов: {elapsed:.4f} с на этой машине; это локальный замер,
не обещание производительности на других нагрузках.</p>
<p><a href="points.csv">Все численные значения CSV</a> · <a href="summary.json">Проверки и SHA256</a>
· <a href="../../tests/fixtures/cusum-cpu-20261122-3h-15s.json">Исходный снимок</a>
· <a href="../cusum/report.html#cpu-3h-15s">Основной отчёт CUSUM</a></p></html>'''
    (output/'report.html').write_text(text,encoding='utf-8')
    print(json.dumps(summary),flush=True)


if __name__=='__main__':
    main()
