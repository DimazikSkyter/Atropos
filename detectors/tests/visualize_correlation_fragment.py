"""Standalone report for a fragment, comparing a fresh start with prior stream state."""
import argparse
import base64
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from tests.correlation_change_replay import MSK
from tests.correlation_fragment_replay import START,END,TRACES,load_fragment,summarize

COLORS={'blue':'#5794f2','orange':'#ff9830','red':'#f2495c','purple':'#b877d9'}
MODES={'cold':('New detector at fragment start','#dc2626','-'),
       'continued':('Continuous detector with preceding history','#2563eb','--')}


def create_report(output,case=None,test_result=None):
    case=load_fragment() if case is None else case
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    report=summarize(case)
    if test_result is not None:report['execution_tests']=test_result
    dates=[datetime.fromtimestamp(int(t),MSK) for t in case.stamps]
    fig=plt.figure(figsize=(16,14),layout='constrained')
    grid=fig.add_gridspec(8,1,height_ratios=[3,.6,2,.6,1.7,.6,1.2,.6])
    axes=[fig.add_subplot(grid[i*2,0]) for i in range(4)]
    slots=[fig.add_subplot(grid[i*2+1,0]) for i in range(4)]
    for i,name in enumerate(TRACES):axes[0].plot(dates,case.values[:,i],label=name,color=COLORS[name],lw=1.2)
    axes[0].set_ylabel('ops/s')
    axes[0].set_title('read_operations fragment: 18 Oct 19:40 — 19 Oct 00:10, Moscow')
    for mode,(label,color,style) in MODES.items():
        rows=case.cold if mode=='cold' else case.continued
        axes[1].plot(dates,[r.score if r.ready else np.nan for r in rows],label=label,color=color,ls=style,lw=1.5)
        axes[2].plot(dates,[max((p.relative_error for p in r.pairs if p.relative_error is not None),default=np.nan)*100
                           if r.ready else np.nan for r in rows],label=label,color=color,ls=style)
        axes[3].step(dates,[sum(p.monitored for p in r.pairs) if not r.pending else np.nan for r in rows],
                     label=label,color=color,ls=style,where='post')
    axes[1].axhline(case.settings['alert_threshold'],color='black',ls=':',label='Alert threshold 0.4')
    axes[1].set_ylabel('Score')
    axes[1].set_ylim(-.02,1.02)
    axes[2].set_ylabel('Max pair relative mismatch, %')
    axes[2].set_ylim(bottom=0)
    axes[3].set_ylabel('Established pairs')
    axes[3].set_ylim(-.2,6.2)
    axes[3].set_yticks(range(7))
    first=next(i for i,r in enumerate(case.cold) if r.ready)
    full=next(i for i,r in enumerate(case.cold) if r.ready and not r.provisional)
    axes[1].axvspan(dates[0],dates[first],color='#64748b',alpha=.12,label='Fresh-start no-score interval')
    axes[1].axvspan(dates[first],dates[full],color='#3b82f6',alpha=.07,label='Fresh-start growing window')
    for ax in axes:
        ax.grid(alpha=.2)
        ax.set_xlim(dates[0],dates[-1])
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m %H:%M',tz=MSK))
    for ax,slot in zip(axes,slots):
        slot.set_axis_off()
        handles,labels=ax.get_legend_handles_labels()
        slot.legend(handles,labels,loc='center',ncol=2,frameon=False,fontsize=9)
    fig.canvas.draw()
    renderer=fig.canvas.get_renderer()
    for slot in slots:
        box=slot.get_legend().get_window_extent(renderer)
        assert all(not box.overlaps(ax.get_window_extent(renderer)) for ax in axes)
    image=output/'fragment.png'
    fig.savefig(image,dpi=135)
    plt.close(fig)
    with (output/'influx-input.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.writer(stream)
        writer.writerow(('timestamp',*TRACES))
        writer.writerows((t.isoformat(),*v) for t,v in zip(dates,case.values))
    report['input_csv_sha256']=hashlib.sha256((output/'influx-input.csv').read_bytes()).hexdigest()
    for mode in MODES:
        rows=case.cold if mode=='cold' else case.continued
        source=case.stamps if mode=='cold' else case.prefix_stamps
        with (output/f'{mode}-scores.csv').open('w',newline='',encoding='utf-8') as stream:
            writer=csv.writer(stream)
            writer.writerow(('timestamp','issued_at','ready','pending','provisional','score','raw_score',
                             'detected','established_pairs','affected_series'))
            writer.writerows((t.isoformat(),datetime.fromtimestamp(int(source[r.observed_index]),MSK).isoformat()
                if r.observed_index is not None else '',r.ready,r.pending,r.provisional,r.score if r.ready else '',
                r.raw_score if r.ready else '',r.detected,sum(p.monitored for p in r.pairs),
                '|'.join(TRACES[i] for i in r.affected_series)) for t,r in zip(dates,rows))
        with (output/f'{mode}-pairs.csv').open('w',newline='',encoding='utf-8') as stream:
            writer=csv.writer(stream)
            writer.writerow(('timestamp','pair','monitored','reference_lag','alignment_shift','raw_r','aligned_r',
                             'reference_r','relative_error','relative_weight','correlation_score','pair_score'))
            writer.writerows((t.isoformat(),'/'.join(TRACES[i] for i in p.pair),p.monitored,p.reference_lag,
                p.alignment_shift,p.raw_correlation,p.aligned_correlation,p.reference_correlation,p.relative_error,
                p.relative_weight,p.correlation_score,p.score) for t,r in zip(dates,rows) for p in r.pairs)
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    a,b=report['runs']['cold'],report['runs']['continued']
    table=''.join(f"<tr><td>{label}</td><td>{a[key]}</td><td>{b[key]}</td></tr>" for label,key in
        [('Первая оценка: время точки','first_score_target'),('Первая оценка: время выдачи','first_score_issued'),
         ('Первая подтверждённая пара','first_established_pair_target'),('Первая тревога: время точки','first_alarm_target'),
         ('Первая тревога: время выдачи','first_alarm_issued'),('Максимальный скор','max_score'),
         ('Точек с тревогой','alarm_points'),('Точек со скором','scored_points')])
    encoded=base64.b64encode(image.read_bytes()).decode()
    validation=''
    if test_result is not None:
        validation=f"<p>Проверки выполнения: {test_result['tests']} тестов, ошибок {test_result['errors']}, неуспешных assertions {test_result['failures']}.</p>"
    html=f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Отдельный прогон участка read_operations</title>
      <style>body{{max-width:1450px;margin:30px auto;padding:0 24px;font:17px/1.5 system-ui;color:#182238}}
      img{{width:100%;height:auto}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #cbd5e1;padding:8px;text-align:left}}
      code{{background:#f1f5f9;padding:3px}}a{{color:#2563eb}}</style>
      <h1>Отдельный прогон выбранного участка</h1>
      <p>По отметкам времени на скриншоте выбран отдельный диапазон: <b>18.10.2026 19:40 — 19.10.2026 00:10, Москва</b>.
      Четыре ряда, по 271 точке, шаг 60 с. Входы прочитаны непосредственно из InfluxDB Atropos;
      CSV не используется для подмены недоступной БД.</p>
      <p><b>Новый старт:</b> новый детектор получает только этот участок с первой точки, без fit и прошлого контекста.
      <b>Непрерывный поток:</b> тот же детектор сначала обрабатывает 55 предшествующих точек, начиная с 18:45,
      затем показан только выбранный фрагмент. Это предшествующая история, а не размеченный нормальный участок.
      Обе ветви заканчивают наблюдение в 00:10; последняя оценка остаётся pending.</p>
      <p>Алгоритм и параметры не менялись: window=32, reference_window=128, max_lag=4,
      alignment_tolerance=1, relative_change_scale=0.1, порог тревоги=0.4, persistence=2.
      Отдельный запуск выдаёт первую оценку для 19:44 в 19:45. Время точки и время выдачи разделены в CSV.</p>
      <table><thead><tr><th>Показатель</th><th>Новый старт на участке</th><th>С предшествующей историей</th></tr></thead>
      <tbody>{table}</tbody></table>{validation}
      <img alt="Четыре ряда и сравнение двух состояний детектора" src="data:image/png;base64,{encoded}">
      <p><b>Наблюдение:</b> на просадке красного ряда около 21:40 новый старт даёт
      {a['regions']['red_excursion']['max_score']:.6f}, то есть чуть ниже 0.4; тревоги нет.
      С предшествующим состоянием максимум {b['regions']['red_excursion']['max_score']:.6f}, тревога есть.
      На позднем расхождении максимумы соответственно {a['regions']['purple_divergence']['max_score']:.6f}
      и {b['regions']['purple_divergence']['max_score']:.6f}.</p>
      <p>Это показывает зависимость чувствительности от выученного состояния. Пройденные тесты проверяют
      корректность прогона, границы и причинность; они не превращают пропуск красной просадки новым запуском
      в успешное обнаружение. Порог не подбирался заново под участок. Участники максимальной пары не являются
      доказанной причиной сбоя; в режиме нового старта на пике это orange/red, с контекстом — blue/purple.</p>
      <p>Данные — оцифровка изображения с перекрытиями и интерполяцией, без независимой разметки аварий.
      Результат не является оценкой precision/recall.</p>
      <p><a href="summary.json">Настройки и сводка</a> · <a href="influx-input.csv">Входы из InfluxDB</a> ·
      <a href="cold-scores.csv">Скор нового старта</a> · <a href="continued-scores.csv">Скор непрерывного потока</a> ·
      <a href="cold-pairs.csv">Пары: новый старт</a> · <a href="continued-pairs.csv">Пары: с историей</a></p></html>'''
    (output/'report.html').write_text(html,encoding='utf-8')
    print(json.dumps({'report':str(output/'report.html'),'runs':report['runs']},indent=2))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('artifacts/correlation-fragment'))
    create_report(parser.parse_args().output)
