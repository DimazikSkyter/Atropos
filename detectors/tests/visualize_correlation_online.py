"""Relative-level, scrape-skew-aware correlation replay from live InfluxDB."""
import base64
import csv
from datetime import datetime
import hashlib
import inspect
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from apps.detectors import CorrelationDetector
from tests.correlation_change_replay import COMMON,MSK,SETTINGS,replay,summary,align_results
from tests.influx_correlation_data import CASES

COLORS={'blue':'#5794f2','green':'#73bf69','yellow':'#c7a000',
        'orange':'#ff9830','red':'#f2495c','purple':'#b877d9'}


def clock(t):
    return datetime.fromtimestamp(int(t),MSK)


def encoded(path):
    return 'data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()


def legend_below(plot,slot,*,ncol=3,fontsize=9):
    handles,labels=plot.get_legend_handles_labels()
    slot.set_axis_off()
    slot.legend(handles,labels,loc='center',ncol=ncol,fontsize=fontsize,frameon=False)


def panel_figure(heights,figsize):
    fig=plt.figure(figsize=figsize,layout='constrained')
    ratios=[]
    for height in heights:ratios.extend((height,.65))
    grid=fig.add_gridspec(2*len(heights),1,height_ratios=ratios)
    return fig,[fig.add_subplot(grid[2*i,0]) for i in range(len(heights))],\
        [fig.add_subplot(grid[2*i+1,0]) for i in range(len(heights))]


def shade(ax,dates,rows):
    first=next(i for i,r in enumerate(rows) if r.ready)
    full=next(i for i,r in enumerate(rows) if r.ready and not r.provisional)
    ax.axvspan(dates[0],dates[first],color='#64748b',alpha=.15,label='Initial no-score interval')
    ax.axvspan(dates[first],dates[full],color='#3b82f6',alpha=.10,label='Growing-window estimate')


def score_plot(ax,dates,rows,raw,threshold):
    ax.plot(dates,[r.score if r.ready else np.nan for r in raw],color='#64748b',ls='--',lw=1,
            label='CCF only: no relative weight / no timing tolerance')
    ax.plot(dates,[r.score if r.ready else np.nan for r in rows],color='#dc2626',label='Relative score, timing tolerance +/-1')
    ax.axhline(threshold,color='black',ls=':',label='Alert threshold')
    ax.set_ylabel('Score')
    ax.set_ylim(-.02,1.05)


def region(stamps,rows,start,stop):
    selected=[r for t,r in zip(stamps,rows) if r.ready and
              datetime.fromisoformat(start).timestamp()<=t<datetime.fromisoformat(stop).timestamp()]
    return {'scored_points':len(selected),'alerts':sum(r.detected for r in selected),
            'max_score':max((r.score for r in selected),default=0)}


def paired_value(row,k,attribute):
    return getattr(row.pairs[k],attribute) if row.ready and len(row.pairs)>k and row.pairs[k].monitored else None


def create_report(output):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    summaries,sections=[],[]
    for case in CASES:
        stamps,values,_,rows=replay(case)
        names,cfg=CASES[case],SETTINGS[case]
        dates=[clock(t) for t in stamps]
        baseline_cfg={**cfg,'alignment_tolerance':0,'relative_change_scale':0.}
        raw=align_results(CorrelationDetector(**baseline_cfg).replay(values),len(values))
        common=np.column_stack([values[:,0]+10*i for i in range(values.shape[1])])
        control=CorrelationDetector(**cfg).replay(common)
        entry=summary(case,stamps,0,rows)
        entry['common_change_control_max_score']=max(r.score for r in control)
        entry['unscored_points']=sum(not r.ready for r in rows)
        if case=='latency':
            windows={'early_visible_region':('2026-10-18T16:53:45+03:00','2026-10-18T17:03:30+03:00'),
                     'yellow_divergence_region':('2026-10-18T17:04:00+03:00','2026-10-18T17:09:00+03:00')}
        else:
            windows={'early_difference_region':('2026-10-18T19:00:00+03:00','2026-10-18T19:26:00+03:00'),
                     'common_drop_region':('2026-10-18T19:28:00+03:00','2026-10-18T19:40:00+03:00'),
                     'quiet_region':('2026-10-18T20:15:00+03:00','2026-10-18T21:20:00+03:00')}
        for key,(start,stop) in windows.items():
            entry[key]={'current':region(stamps,rows,start,stop),'ccf_only':region(stamps,raw,start,stop)}
        last=next(r for r in reversed(rows) if r.ready)
        keys=[p.pair for p in last.pairs]
        fig,axes,slots=panel_figure([3,1.7,1.6,2,2],(16,18))
        for i,name in enumerate(names):axes[0].plot(dates,values[:,i],color=COLORS[name],label=name,lw=1)
        axes[0].set_title(case+' — relative interaction and scrape skew, from InfluxDB')
        axes[0].set_ylabel('ms' if case=='latency' else 'ops/s')
        shade(axes[0],dates,rows)
        score_plot(axes[1],dates,rows,raw,cfg['alert_threshold'])
        axes[1].plot(dates,[r.score if r.ready else np.nan for r in align_results(control,len(values))],
                     color='#15803d',ls=':',label='Common-signal control')
        for k,key in enumerate(keys):
            label='/'.join(names[i] for i in key)
            error=[paired_value(r,k,'relative_error') for r in rows]
            axes[2].plot(dates,[v*100 if v is not None else np.nan for v in error],label=label,color=f'C{k}',lw=.9)
            axes[3].plot(dates,[paired_value(r,k,'aligned_correlation') for r in rows],label=label,color=f'C{k}',lw=.9)
            axes[3].plot(dates,[paired_value(r,k,'raw_correlation') for r in rows],color=f'C{k}',ls=':',alpha=.25,lw=.7)
            axes[3].plot(dates,[paired_value(r,k,'reference_correlation') for r in rows],color=f'C{k}',ls='--',alpha=.6,lw=.7)
        axes[2].set_ylabel('Relative mismatch, %')
        axes[2].set_ylim(bottom=0)
        axes[3].plot([],[],color='black',ls=':',alpha=.5,label='Unaligned r')
        axes[3].plot([],[],color='black',ls='--',label='Prior reference r')
        axes[3].set_ylabel('Aligned correlation r')
        axes[3].set_ylim(-1.05,1.05)
        peak=max((i for i,r in enumerate(rows) if r.ready),key=lambda i:rows[i].score)
        pair=max(rows[peak].pairs,key=lambda p:p.score)
        lags=np.arange(-cfg['max_lag'],cfg['max_lag']+1)
        axes[4].plot(lags,pair.reference_ccf,'o-',label='Prior reference CCF')
        axes[4].plot(lags,[np.nan if v is None else v for v in pair.ccf],'o-',label='Raw CCF (unaligned)')
        if pair.reference_lag is not None:
            q=pair.reference_lag
            axes[4].axvspan(q-cfg['alignment_tolerance'],q+cfg['alignment_tolerance'],color='#64748b',alpha=.1,label='Allowed timing tolerance')
            if pair.aligned_correlation is not None:
                axes[4].scatter([q],[pair.aligned_correlation],color='#15803d',marker='s',zorder=5,
                                label='Aligned-window r; shift '+str(pair.alignment_shift))
        axes[4].set_title('/'.join(names[i] for i in pair.pair)+' — target '+dates[peak].strftime('%d.%m %H:%M:%S'))
        axes[4].set_xlabel('Lag, samples (aligned scalar plotted at the reference lag)')
        axes[4].set_ylabel('CCF / aligned estimate')
        axes[4].set_ylim(-1.05,1.05)
        for ax in axes[:4]:
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m %H:%M',tz=MSK))
            ax.set_xlim(dates[0],dates[-1])
        for ax,slot in zip(axes,slots):
            ax.grid(alpha=.2)
            legend_below(ax,slot)
        image=output/f'{case}.png'
        fig.savefig(image,dpi=135)
        plt.close(fig)
        with (output/f'{case}-scores.csv').open('w',newline='',encoding='utf-8') as stream:
            writer=csv.writer(stream)
            writer.writerow(('timestamp','issued_at','ready','pending','provisional','score','raw_score',
                             'detected','monitored_pairs','affected_series','score_index','observed_index'))
            writer.writerows((t.isoformat(),dates[r.observed_index].isoformat() if r.observed_index is not None else '',
                r.ready,r.pending,r.provisional,r.score if r.ready else '',r.raw_score if r.ready else '',r.detected,
                sum(p.monitored for p in r.pairs),'|'.join(names[i] for i in r.affected_series),r.score_index,
                r.observed_index) for t,r in zip(dates,rows))
        with (output/f'{case}-pairs.csv').open('w',newline='',encoding='utf-8') as stream:
            writer=csv.writer(stream)
            writer.writerow(('timestamp','pair','monitored','reference_lag','alignment_shift','raw_r','aligned_r',
                             'reference_r','relative_error','relative_weight','correlation_score','pair_score'))
            writer.writerows((t.isoformat(),'/'.join(names[i] for i in p.pair),p.monitored,p.reference_lag,
                p.alignment_shift,p.raw_correlation,p.aligned_correlation,p.reference_correlation,p.relative_error,
                p.relative_weight,p.correlation_score,p.score) for t,r in zip(dates,rows) for p in r.pairs)
        with (output/f'{case}-influx-input.csv').open('w',newline='',encoding='utf-8') as stream:
            writer=csv.writer(stream)
            writer.writerow(('timestamp',*names))
            writer.writerows((t.isoformat(),*row) for t,row in zip(dates,values))
        entry['input_csv_sha256']=hashlib.sha256((output/f'{case}-influx-input.csv').read_bytes()).hexdigest()
        zoom_name='latency-startup.png' if case=='latency' else 'read-operations-start.png'
        count=2 if case=='latency' else 3
        zoom,za,zs=panel_figure([2.5,1.6] if count==2 else [2.5,1.6,1.5],(16,7.5 if count==2 else 11))
        stop=datetime.fromisoformat('2026-10-18T17:03:30+03:00' if case=='latency' else '2026-10-18T19:45:00+03:00')
        indices=[i for i,t in enumerate(dates) if t<=stop]
        for i,name in enumerate(names):za[0].plot([dates[k] for k in indices],values[indices,i],label=name,color=COLORS[name])
        za[0].set_ylabel('ms' if case=='latency' else 'ops/s')
        za[0].set_title(case+' — early divergence and common fall; all score timestamps retained')
        shade(za[0],dates,rows)
        score_plot(za[1],dates,rows,raw,cfg['alert_threshold'])
        if case!='latency':
            start_drop=datetime.fromisoformat('2026-10-18T19:28:00+03:00')
            for ax in za[:2]:ax.axvspan(start_drop,datetime.fromisoformat('2026-10-18T19:40:00+03:00'),color='#16a34a',alpha=.09,label='Common decline')
            for k,key in enumerate(keys):
                vals=[paired_value(rows[j],k,'relative_error') for j in indices]
                za[2].plot([dates[j] for j in indices],[v*100 if v is not None else np.nan for v in vals],
                           label='/'.join(names[i] for i in key),color=f'C{k}')
            za[2].set_ylabel('Relative mismatch, %')
            za[2].set_ylim(bottom=0)
        for ax,slot in zip(za,zs):
            ax.grid(alpha=.2)
            ax.set_xlim(dates[0],stop)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S',tz=MSK))
            legend_below(ax,slot)
        zoom.savefig(output/zoom_name,dpi=135)
        plt.close(zoom)
        sections.append(f'''<section><h2>{case}</h2><p>Первая тревога относится к {entry['first_alarm']};
          выдана в {entry['first_alarm_issued']}. Максимум score: {entry['max_score']:.3f}.
          Оценок: {entry['scored_observations']} из {len(values)}; последняя точка ожидает правого соседа.
          Шаг: {entry['step_seconds']} с; задержка выдачи: {entry['alignment_delay_seconds']} с.</p>
          <img alt="Данные, скор, относительное расхождение и корреляция с допуском" src="{encoded(image)}">
          <h3>Начало крупно</h3><img alt="Ранний участок и общий спад" src="{encoded(output/zoom_name)}">
          <p><a href="{case}-scores.csv">Скор и время выдачи</a> · <a href="{case}-pairs.csv">Компоненты балла пар</a> ·
          <a href="{case}-influx-input.csv">Входы из InfluxDB</a></p></section>''')
        summaries.append(entry)
    report={'mode':'online relative mismatch, delayed +/-1 timing tolerance','source':'live InfluxDB queries',
            'generated_at':datetime.now(MSK).isoformat(),'common_settings':COMMON,
            'detector_sha256':hashlib.sha256(inspect.getsource(CorrelationDetector).encode()).hexdigest(),
            'scenarios':summaries}
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    html='''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Относительное расхождение и рассинхронизация</title>
      <style>body{max-width:1450px;margin:30px auto;padding:0 24px;font:17px/1.5 system-ui;color:#182238}
      img{width:100%;height:auto}section{margin:35px 0}code{background:#f1f5f9;padding:3px}a{color:#2563eb}</style>
      <h1>Изменение связи с учётом уровня рядов и времени сбора</h1>
      <p>Полный онлайн-прогон с первой точки; fit и ручные нормальные интервалы не используются.
      Все входы прочитаны прямо из InfluxDB Atropos:
      <code>metrics-replay / correlation_screenshot_replay</code>. Исходные значения и даты сохранены.</p>
      <p>Малое расхождение на большом уровне должно весить меньше:
      15/400 = 3.75%, 25/800 = 3.125%. Поэтому изменение корреляции теперь умножается
      на вес относительного нарушения уже выученной связи, а не на величину абсолютного отклонения.</p>
      <p>По принятым прошлым данным пара учит аффинную зависимость в обе стороны.
      Для каждого согласованного отсчёта ошибка делится на модуль ожидаемого <b>текущего</b> уровня;
      около нуля используется фоновое std. e — RMS этих относительных ошибок обоих направлений в окне,
      с насыщением на 200%. Обычное смещение и масштаб пары учитываются прогнозом.
      <code>relative_weight = e / (e + relative_change_scale)</code>, здесь scale=0.1.
      Расхождение 10% даёт вес 0.5; ноль отключает относительный вес.</p>
      <p><code>alignment_tolerance=1</code> разрешает отклонение времени сбора на один отсчёт
      в обе стороны от установленного лага. Для каждой выдаваемой оценки выбирается лучший
      согласованный сдвиг её окна из −1, 0, +1. Все точки окна имеют один сдвиг;
      независимый подбор каждого значения мог бы искусственно скоррелировать шум.
      Сдвиги за пределами допуска не оптимизируются.</p>
      <p>Правая соседняя точка требует задержки выдачи: 15 с у latency, 60 с у read_operations.
      CSV различает timestamp оцениваемой точки и issued_at. Первые четыре точки не имеют оценки;
      для пятой она рассчитывается после минуты наблюдений и выдаётся ещё через один отсчёт.
      Последняя точка файла имеет pending=True, её правый сосед не выдумывается.
      Во время роста окна остальные оценки выдаются непрерывно.</p>
      <p>Эталон, его gate и модель уровня обучаются только на данных до оцениваемой точки включительно,
      после её оценки. Правый сосед участвует только в задержанном сопоставлении и не обучает собственный
      эталон. Новая связь должна подтвердиться в двух непересекающихся частях окна.
      Сильное нарушение блокирует обучение этой пары.</p>
      <p>Пунктир CCF-only показывает тот же онлайн-алгоритм при alignment_tolerance=0 и relative_change_scale=0.
      Красный скор использует обе поправки. Балл изменения корреляции включает порог min_change=0.25
      и вес информативности на плоских участках; затем умножается на relative_weight.
      Общий скор — среднее максимумов пар за persistence=2 обновления. Порог тревоги 0.4 одинаков у обоих примеров.</p>
      <p>Пик около 19:10 предшествует общему спаду около 19:28. Раньше короткая корреляция давала там 1,
      хотя расхождение невелико относительно уровня около 800. Теперь эти интервалы показаны отдельно
      вместе с процентным расхождением. На первом графике заметное расхождение жёлтой линии сохраняет высокий балл.</p>
      <p>Это эвристический скор, не вероятность и не оценка precision/recall. Входы — оцифровка изображений,
      3+4 различимые линии с интерполяцией перекрытий. Очень медленные изменения могут усваиваться моделью;
      начальное нарушение может стать первой выученной связью. Нулевой скор без подтверждённых пар не означает исправность.
      Изменение уровня при полностью сохранённой корреляции само по себе не получает отдельного балла.</p>
      <p><a href="summary.json">Настройки и результаты JSON</a> ·
      <a href="http://localhost:3001/d/correlation-change-20261018">Исходные ряды в Grafana</a></p>'''+''.join(sections)+'</html>'
    (output/'report.html').write_text(html,encoding='utf-8')
    print(json.dumps({'report':str(output/'report.html'),'scenarios':summaries},ensure_ascii=True,indent=2))
    return report
