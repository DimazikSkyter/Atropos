"""Bounded SSA replay on the existing MAD service snapshots, without network."""
import os
for _key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ.setdefault(_key,'1')
import argparse
from dataclasses import asdict
import csv
from datetime import datetime, timedelta
import hashlib
import html
import inspect
import json
from pathlib import Path
import time

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from apps.detectors import SSADetector

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name('fixtures')
STEM = 'acf-service-response-20261005'
CONFIG = dict(window=192,data_max_size=960,lag=None,energy=.95,max_rank=12,
              refit_interval=16,score_scale=1.)


def services():
    source=(FIXTURES/f'{STEM}.csv').read_bytes()
    metadata=json.loads((FIXTURES/f'{STEM}.json').read_text(encoding='utf-8'))
    if hashlib.sha256(source).hexdigest()!=metadata['csv_sha256']:
        raise ValueError('Input snapshot hash mismatch')
    groups={}
    for row in csv.DictReader(source.decode('utf-8').splitlines()):
        groups.setdefault(row['service'],[]).append(row)
    for name,rows in groups.items():
        values=np.array([float(r['value_ms']) for r in rows])
        if hashlib.sha256(values.astype('<f8').tobytes()).hexdigest()!=metadata['services'][name]['values_sha256']:
            raise ValueError('Service value hash mismatch')
        yield name,values,[r['timestamp_utc'] for r in rows],metadata


def replay(name,values,timestamps,config,output):
    detector=SSADetector(**config)
    frozen=SSADetector(**dict(config,refit_interval=None))
    rows=[];durations=[]
    for i,value in enumerate(values):
        before=time.perf_counter()
        score=detector.update(float(value))
        durations.append(time.perf_counter()-before)
        frozen_score=frozen.update(float(value))
        rows.append(dict(index=i,timestamp_utc=timestamps[i] if timestamps else '',
                         value=float(value),**asdict(detector.result),
                         frozen_score=frozen_score,frozen_deformation=frozen.result.deformation))
    with (output/f'{name}.csv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    good=[r for r in rows if r['ready']]
    first=config['data_max_size']-1
    summary=dict(name=name,points=len(values),config=config,first_ready=first,
                 peak_score=max(r['score'] for r in good),
                 frozen_peak_score=max(r['frozen_score'] for r in good),
                 peak_index=max(good,key=lambda r:r['score'])['index'],
                 lag_range=[min(r['lag'] for r in good),max(r['lag'] for r in good)],
                 rank_range=[min(r['rank'] for r in good),max(r['rank'] for r in good)],
                 mean_ms=1000*float(np.mean(durations[first:])),
                 p95_ms=1000*float(np.quantile(durations[first:],.95)))
    if timestamps:
        summary['peak_time_msk']=(datetime.fromisoformat(timestamps[summary['peak_index']].replace('Z','+00:00'))+timedelta(hours=3)).strftime('%H:%M:%S')
        summary['mean_score']={label:float(np.mean([r['score'] for r in rows[lo:hi] if r['ready']]))
                               for label,lo,hi in [('04:00–05:00',960,1200),('05:00–10:00',1200,2400),('10:00–11:50',2400,len(values))]}
        summary['frozen_mean_score']={label:float(np.mean([r['frozen_score'] for r in rows[lo:hi] if r['ready']]))
                               for label,lo,hi in [('04:00–05:00',960,1200),('05:00–10:00',1200,2400),('10:00–11:50',2400,len(values))]}
    plot(name,rows,output,bool(timestamps))
    return summary


def plot(name,rows,output,clock):
    x=np.arange(len(rows))/240
    valid=np.array([r['ready'] for r in rows])
    fig,axes=plt.subplots(4,1,figsize=(13,10),sharex=True,
                         gridspec_kw={'height_ratios':[2,1,1.2,1.2]})
    axes[0].plot(x,[r['value'] for r in rows],color='#4c8445',lw=.85)
    axes[0].set_ylabel('Вход, мс' if clock else 'Вход')
    axes[0].set_title(f'{name}: SSADetector',loc='left',fontsize=16,fontweight='bold')
    axes[1].plot(x[valid],[r['reference_sigma'] for r in rows if r['ready']],color='#e09627',label='σ большого референса')
    axes[1].set_ylabel('Референс, σ')
    axes[2].plot(x[valid],[min(r['deformation'],1e4) for r in rows if r['ready']],color='#225cb2',label='D: SSA-остаток / σ референса')
    axes[2].plot(x[valid],[r['reference_deformation'] for r in rows if r['ready']],color='#888888',ls='--',label='Реконструкционный фон референса')
    axes[2].set_ylabel('До сжатия, D')
    axes[3].plot(x[valid],[r['score'] for r in rows if r['ready']],color='#225cb2',label='score = D / (D + 1)')
    axes[3].plot(x[valid],[r['frozen_score'] for r in rows if r['ready']],color='#17867f',ls='--',label='Та же нормировка, первая модель зафиксирована')
    axes[3].set_ylabel('Score [0,1]');axes[3].set_ylim(-.02,1.02)
    axes[3].set_xlabel('Время МСК, часы' if clock else 'Время от начала, часы; шаг 15 секунд')
    end=np.flatnonzero(valid)[0]
    for ax in axes:
        ax.axvspan(x[0],x[end],color='#64748b',alpha=.1);ax.grid(alpha=.16)
        if ax.get_legend_handles_labels()[0]:ax.legend(loc='upper left',fontsize=8)
    fig.tight_layout();fig.savefig(output/f'{name}.png',dpi=130);plt.close(fig)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'artifacts'/'ssa')
    args=parser.parse_args();output=args.output;output.mkdir(exist_ok=True,parents=True)
    summaries=[];metadata=None
    for name,values,ts,metadata in services():
        s=replay(name,values,ts,CONFIG,output);summaries.append(s)
        print(json.dumps(s,ensure_ascii=False),flush=True)
    rng=np.random.default_rng(43);t=np.arange(2400);p=2*np.pi*t/48
    base=np.sin(p)+.35*np.sin(2*p+.3)
    new=np.sin(p)+.8*np.sin(3*p+.5);new*=base[:1200].std()/new[:1200].std()
    noise=rng.normal(0,.04,len(t))
    synthetic={
        'stable-cycle':base+noise,
        'shape-change':np.where(t<1200,base,new)+noise,
        'level-change':base+noise+np.where(t<1200,0.,3.),
    }
    small=dict(CONFIG,window=96,data_max_size=480,lag=48)
    for name,values in synthetic.items():
        summaries.append(replay(name,values,[],small,output))
    report=dict(config=CONFIG,source=metadata,source_class_sha256=hashlib.sha256(inspect.getsource(SSADetector).encode()).hexdigest(),cases=summaries)
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    body=['<h1>SSADetector: SSA-оценка в [0,1]</h1>',
          '<p>Тот же снимок четырех сервисов из MAD-отчета. Текущее окно: 192 точки / 48 минут. '
          'Предшествующий референс: 768 точек / 3 часа 12 минут. Полная история: 960 точек / 4 часа. '
          'Модель обновляется раз в 16 точек / 4 минуты. Серая зона — прогрев.</p>',
          '<p><b>D = RMS остатка вне SSA-подпространства / σ референса; score = D / (D + 1).</b> '
          'D сохранен без сжатия. Нормировочная σ и модель вычисляются по данным перед текущим окном. '
          'После попадания нового режима в референс он постепенно становится новой нормой. '
          'Это отличается от прежнего эксперимента с постоянной здоровой моделью.</p>',
          '<p>Зеленый пунктир внизу — режим <code>refit_interval=None</code>: первая модель и σ '
          'зафиксированы. Он сохраняет смысл длительного отклонения от исходного режима. '
          'Оба варианта используют одинаковое сжатие [0,1]; разница определяется адаптацией референса.</p>',
          '<p>Score — величина реконструкционного остатка, не вероятность. Стационарный шум может давать ненулевой фон. '
          'Пунктир показывает собственный реконструкционный фон референса; из оценки D он не вычитается. '
          'Смена формы внутри неизменного пространства гармоник может стать незаметной после перехода.</p>',
          '<p>Вход — Influx replay, ранее реконструированный по изображению. Экспертных меток событий нет; '
          'таблица показывает реакцию, а не точность обнаружения аварий.</p>',
          '<table><tr><th>Сервис</th><th>Средний score 04–05</th><th>05–10</th><th>10–11:50</th><th>Максимум</th><th>МСК</th></tr>']
    for s in summaries[:4]:
        m=list(s['mean_score'].values())
        body.append('<tr>'+''.join(f'<td>{v}</td>' for v in [s['name'],*[f'{v:.3f}' for v in m],f"{s['peak_score']:.3f}",s['peak_time_msk']])+'</tr>')
    body.append('</table>')
    body.append('<h2>Поздний участок: скользящий и постоянный референс</h2><table><tr><th>Сервис</th>'
                '<th>Средний score 10–11:50, скользящий</th><th>Постоянный</th></tr>')
    for s in summaries[:4]:
        body.append(f'<tr><td>{s["name"]}</td><td>{list(s["mean_score"].values())[-1]:.3f}</td>'
                    f'<td>{list(s["frozen_mean_score"].values())[-1]:.3f}</td></tr>')
    body.append('</table>')
    for s in summaries:
        name=s['name'];body.append(f'<h2>{html.escape(name)}</h2><img src="{name}.png" alt="{name}">'
                                  f'<p><a href="{name}.csv">CSV всех точек</a>. '
                                  f'Лаг {s["lag_range"]}, ранг {s["rank_range"]}. Среднее время точки '
                                  f'{s["mean_ms"]:.3f} мс, p95 {s["p95_ms"]:.3f} мс; один локальный прогон.</p>')
    body.append('<p><a href="summary.json">Параметры и результаты JSON</a> · '
                '<a href="../../SSA.md">Описание API и ограничений</a></p>')
    css='body{font:16px/1.6 system-ui;margin:36px auto;max-width:1200px;padding:0 24px;color:#243247}img{max-width:100%}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}h2{margin-top:40px}a{color:#225cb2}'
    (output/'report.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><title>SSADetector</title><style>'+css+'</style>'+''.join(body)+'</html>',encoding='utf-8')
    print(output/'report.html',flush=True)


if __name__=='__main__':main()
