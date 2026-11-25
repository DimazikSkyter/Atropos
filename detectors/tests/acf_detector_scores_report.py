"""ACF over saved detector SCORES, with actual issue times and two warmups.

Inputs are immutable snapshots from the three report screenshots, not digitized
again. Core detectors, thresholds and existing reports are never modified.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import argparse
import csv
import hashlib
import html
import inspect
import json
from pathlib import Path
import shutil

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from apps.detectors import ACFDetector

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=Path(__file__).with_name('fixtures')/'acf-detector-scores'
MSK=timezone(timedelta(hours=3))
PROFILES={
    'default':dict(window=256,max_lag=32,reference_window=512,threshold=3.5,min_change=.1),
    'short':dict(window=40,max_lag=10,reference_window=160,threshold=3.5,min_change=.1),
    'short_reference':dict(window=40,max_lag=10,reference_window=40,threshold=3.5,min_change=.1),
    'compact':dict(window=16,max_lag=4,reference_window=32,threshold=2.,min_change=.1),
}
COLORS={'default':'#b85a18','short':'#2563eb','short_reference':'#64748b','compact':'#8b45a3'}
LABELS={'default':'ACF 256 / фон 512 / лаг 32',
        'short':'ACF 40 / фон 160 / лаг 10',
        'short_reference':'ACF 40 / фон 40 / лаг 10',
        'compact':'ACF 16 / фон 32 / лаг 4; threshold=2'}
SPECS=(
    ('cusum-orange-default','CUSUM 10/200 → ACF','cusum-orange-default.csv','cusum-default'),
    ('cusum-orange-wide','CUSUM 40/800 → ACF','cusum-orange-wide.csv','cusum-wide'),
    ('correlation-latency','Correlation: относительный score → ACF','correlation-latency.csv','correlation'),
    ('correlation-latency-ccf','Correlation: CCF-only, пунктир на скриншоте → ACF','correlation-latency-ccf.csv','correlation-ccf'),
    ('trend-cassandra-1','Trend: накопленный score Cassandra-1 → ACF','trend-cassandra-1.csv','trend'),
)


def read_csv(path):
    with path.open(encoding='utf-8',newline='') as f:return list(csv.DictReader(f))


def write_csv(path,rows):
    with path.open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


def write_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(MSK)


@dataclass
class ScoreTrace:
    name:str
    title:str
    config:dict
    rows:list
    available:list
    source_times:tuple
    source_values:np.ndarray
    source_labels:tuple
    input_file:str

    @property
    def values(self):return np.array([r['score'] for r in self.available],dtype=float)

    @property
    def times(self):return tuple(parse_time(r['issued_at']) for r in self.available)


def load_traces(fixtures=FIXTURES):
    fixtures=Path(fixtures)
    meta=json.loads((fixtures/'manifest.json').read_text(encoding='utf-8'))
    for filename,record in meta['files'].items():
        if hashlib.sha256((fixtures/filename).read_bytes()).hexdigest()!=record['sha256']:
            raise ValueError('Score snapshot changed: '+filename)
    result=[]
    for name,title,filename,key in SPECS:
        original=read_csv(fixtures/filename)
        rows=[]
        for i,r in enumerate(original):
            correlation=name.startswith('correlation')
            score_at=r['timestamp'] if correlation else r['timestamp_msk']
            issued=r['issued_at'] if correlation else score_at
            ready=r['ready']=='True' and bool(r['score']) and bool(issued)
            rows.append(dict(source_index=i,score_at=score_at,issued_at=issued,
                             lower_ready=ready,lower_pending=r.get('pending')=='True',
                             lower_provisional=r.get('provisional')=='True',
                             score=float(r['score']) if ready else None))
        available=[r for r in rows if r['lower_ready']]
        for r in available:
            if not 0<=r['score']<=1 or not np.isfinite(r['score']):raise ValueError('Invalid source score')
            if parse_time(r['issued_at'])<parse_time(r['score_at']):raise ValueError('Score issued before its source point')
        times=[parse_time(r['issued_at']) for r in available]
        if any((b-a).total_seconds()!=15 for a,b in zip(times,times[1:])):
            raise ValueError('Unavailable source scores must not be interpolated or compressed')
        if name.startswith('correlation'):
            raw=read_csv(fixtures/'latency-input.csv')
            source_times=tuple(parse_time(r['timestamp']) for r in raw)
            source_labels=('blue','green','yellow')
            source_values=np.array([[float(r[k]) for k in source_labels] for r in raw])
        else:
            source_times=tuple(parse_time(r['timestamp_msk']) for r in original)
            source_labels=('TPS',) if name.startswith('cusum') else ('Время отклика, мс',)
            source_values=np.array([[float(r['value'])] for r in original])
        result.append(ScoreTrace(name,title,meta['lower_configs'][key],rows,available,
                                 source_times,source_values,source_labels,filename))
    return result,meta


def replay_acf(trace,config):
    """Feed only available scores in issue order. No refit or synthetic history."""
    detector=ACFDetector(**config)
    records=[];snapshots=[]
    for i,r in enumerate(trace.available):
        detector.update(r['score']);s=detector.result
        phase='reference' if i<config['reference_window'] else ('scoring' if s.ready else 'local_warmup')
        records.append(dict(**r,upper_index=i,phase=phase,acf_ready=s.ready,
                            acf_score=s.score if s.ready else None,depth=s.depth if s.ready else None,
                            depth_seconds=15*s.depth if s.ready else None,
                            lag_mass=s.lag_mass if s.ready else None,
                            horizon_reached=s.horizon_reached if s.ready else None,
                            zero_energy=s.zero_energy if s.ready else None))
        snapshots.append(s)
    return records,snapshots


def describe(trace,config,records,snapshots):
    ready=[r for r in records if r['acf_ready']]
    peak=max(ready,key=lambda r:r['acf_score']) if ready else None
    positive=[r for r in ready if r['acf_score']>0]
    j=int(np.argmax(trace.values));s=snapshots[j]
    input_peak=dict(score=float(trace.values[j]),issued_at=records[j]['issued_at'],
                    acf_ready=s.ready,acf_score=s.score if s.ready else None,
                    current_lag1=s.acf[1] if s.ready else None,
                    reference_lag1=s.reference_acf[1] if s.ready else None,
                    band_lag1=s.thresholds[1] if s.ready else None)
    return dict(config=config,required_ready_scores=config['reference_window']+config['window'],
                available_lower_scores=len(records),ready_points=len(ready),
                missing_before_first_ready=max(0,config['reference_window']+config['window']-len(records)),
                first_ready_issued_at=ready[0]['issued_at'] if ready else None,
                first_positive_issued_at=positive[0]['issued_at'] if positive else None,
                max_score=peak['acf_score'] if peak else None,
                lower_score_at_acf_peak=peak['score'] if peak else None,
                zero_energy_at_acf_peak=peak['zero_energy'] if peak else None,
                peak_upper_index=peak['upper_index'] if peak else None,
                peak_source_index=peak['source_index'] if peak else None,
                peak_issued_at=peak['issued_at'] if peak else None,
                depth_at_peak=peak['depth'] if peak else None,
                max_depth=max((r['depth'] for r in ready),default=None),
                max_depth_seconds=max((r['depth_seconds'] for r in ready),default=None),
                nonzero_points=len(positive),horizon_reached_points=sum(r['horizon_reached'] for r in ready),
                zero_energy_points=sum(r['zero_energy'] for r in ready),
                reference_zero_energy=bool(snapshots[-1].reference_zero_energy) if ready else None,
                last_zero_energy=bool(snapshots[-1].zero_energy) if ready else None,
                last_acf_score=snapshots[-1].score if ready else None,
                at_input_score_peak=input_peak)


def plot_trace(trace,replays,stats,output,zoom=False):
    fig,axes=plt.subplots(4,1,figsize=(14,11),sharex=True,
                          gridspec_kw={'height_ratios':[2,1.2,1.2,1.2]},layout='constrained')
    for i,label in enumerate(trace.source_labels):
        color={'blue':'#5794f2','green':'#73bf69','yellow':'#c7a000'}.get(label,'#d58b27')
        axes[0].plot(trace.source_times,trace.source_values[:,i],label=label,lw=.8,color=color)
    axes[0].set_title(trace.title+(' — участок реакции' if zoom else ''),loc='left',fontsize=15,fontweight='bold')
    axes[0].set_ylabel('Исходные TPS' if trace.name.startswith('cusum') else 'Исходная метрика, мс')
    axes[0].legend(fontsize=8,ncol=3,loc='upper left')
    axes[1].plot(trace.times,trace.values,color='#dc2626',lw=.9,label='Вход ACF: score нижнего детектора по времени выдачи')
    axes[1].set_ylim(-.03,1.04);axes[1].set_ylabel('Входной score')
    axes[1].legend(fontsize=8,loc='upper left')
    chosen=('compact','short_reference') if trace.name.startswith('correlation') else ('default','short','compact')
    for key in chosen:
        records,_=replays[key];t=[parse_time(r['issued_at']) for r in records]
        axes[2].plot(t,[r['acf_score'] if r['acf_ready'] else np.nan for r in records],
                     color=COLORS[key],lw=1,label=LABELS[key])
        axes[3].plot(t,[r['depth_seconds'] if r['acf_ready'] else np.nan for r in records],
                     color=COLORS[key],lw=.9,drawstyle='steps-post',label=LABELS[key])
    axes[2].set_ylim(-.02,1.03);axes[2].set_ylabel('Балл ACF [0,1]')
    axes[2].legend(fontsize=8,ncol=1 if trace.name.startswith('correlation') else 2,loc='upper left')
    if trace.name.startswith('correlation'):
        axes[2].text(.01,.62,'ACF 40/160 и 256/512: недостаточно истории',transform=axes[2].transAxes,fontsize=9,color='#64748b')
    axes[3].set_ylim(-5,max(PROFILES[k]['max_lag']*15 for k in chosen)*1.15)
    axes[3].set_ylabel('Глубина ACF, с')
    axes[3].set_xlabel('Время фактической доступности, МСК; шаг 15 секунд')
    for ax in axes:
        ax.grid(alpha=.16)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK,minticks=4,maxticks=8))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m\n%H:%M',tz=MSK))
        ax.set_xlim(trace.source_times[0],trace.source_times[-1])
    first=trace.times[0]
    axes[0].axvspan(trace.source_times[0],first,color='#64748b',alpha=.14)
    primary='compact' if trace.name.startswith('correlation') else 'short'
    available_starts=[parse_time(stats[k]['first_ready_issued_at']) for k in chosen if stats[k]['first_ready_issued_at']]
    if available_starts:
        end=min(available_starts)
        for ax in axes[2:]:ax.axvspan(first,end,color='#64748b',alpha=.09)
    if zoom:
        key='default'
        peak=stats[key]['peak_issued_at'] or stats[primary]['peak_issued_at']
        center=parse_time(peak)
        for ax in axes:
            ax.set_xlim(max(trace.source_times[0],center-timedelta(minutes=90)),
                        min(trace.source_times[-1],center+timedelta(minutes=90)))
    name=trace.name+('-zoom' if zoom else '')+'.png'
    fig.savefig(output/name,dpi=125);plt.close(fig)


def plot_profiles(trace,replays,stats,output):
    fig,axes=plt.subplots(1,4,figsize=(16,4.3),sharey=True,layout='constrained')
    for ax,(key,cfg) in zip(axes,PROFILES.items()):
        m=stats[key]
        ax.set_title(f'{key}: {cfg["window"]}/{cfg["reference_window"]}',fontsize=11)
        if m['peak_upper_index'] is None:
            ax.text(.5,.5,'Нет готового результата',ha='center',va='center',transform=ax.transAxes)
        else:
            r=replays[key][1][m['peak_upper_index']]
            lag=np.arange(1,cfg['max_lag']+1)
            ref=np.array(r.reference_acf[1:]);band=np.array(r.thresholds[1:])
            ax.fill_between(lag,np.maximum(-1,ref-band),np.minimum(1,ref+band),color='#64748b',alpha=.16)
            ax.plot(lag,r.acf[1:],color=COLORS[key],label='Текущий ACF')
            ax.plot(lag,ref,color='#e09622',label='Фон')
            if r.significant_lags:
                idx=np.array(r.significant_lags);ax.scatter(idx,[r.acf[k] for k in idx],s=18,color='#dc2626')
            ax.text(.02,.03,f'peak score={r.score:.5f}; depth={r.depth}',transform=ax.transAxes,fontsize=8)
        ax.set_ylim(-1.05,1.05);ax.grid(alpha=.15);ax.set_xlabel('Лаг, точки × 15 с')
    axes[0].set_ylabel('ACF при максимуме балла')
    fig.savefig(output/(trace.name+'-profiles.png'),dpi=125);plt.close(fig)


def profile_csv(snapshot):
    return [dict(lag=k,lag_seconds=15*k,acf=snapshot.acf[k],reference_acf=snapshot.reference_acf[k],
                 difference=snapshot.lag_changes[k],band=snapshot.thresholds[k],significant=k in snapshot.significant_lags)
            for k in range(len(snapshot.acf))]


def table(headers,rows):
    return '<table><thead><tr>'+''.join('<th>'+h+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join(
        '<tr>'+''.join('<td>'+str(v)+'</td>' for v in r)+'</tr>' for r in rows)+'</tbody></table>'


def create_report(output,fixtures=FIXTURES):
    output=Path(output);output.mkdir(exist_ok=True,parents=True)
    traces,manifest=load_traces(fixtures)
    inputs=output/'inputs';inputs.mkdir(exist_ok=True)
    for path in Path(fixtures).iterdir():
        if path.is_file():shutil.copy2(path,inputs/path.name)
    cases=[]
    for trace in traces:
        replays={};stats={}
        for key,cfg in PROFILES.items():
            records,snapshots=replay_acf(trace,cfg)
            replays[key]=(records,snapshots);stats[key]=describe(trace,cfg,records,snapshots)
            write_csv(output/f'{trace.name}-{key}.csv',records)
            i=stats[key]['peak_upper_index']
            if i is not None:write_csv(output/f'{trace.name}-{key}-peak-lags.csv',profile_csv(snapshots[i]))
        plot_trace(trace,replays,stats,output)
        plot_profiles(trace,replays,stats,output)
        if not trace.name.startswith('correlation'):plot_trace(trace,replays,stats,output,zoom=True)
        case=dict(name=trace.name,title=trace.title,input_file=trace.input_file,lower_config=trace.config,
                  source_points=len(trace.rows),lower_ready_scores=len(trace.available),
                  lower_pending_points=sum(r['lower_pending'] for r in trace.rows),
                  lower_provisional_points=sum(r['lower_provisional'] and r['lower_ready'] for r in trace.rows),
                  first_lower_score_issued_at=trace.available[0]['issued_at'],
                  source_scores_sha256=hashlib.sha256(trace.values.astype('<f8').tobytes()).hexdigest(),profiles=stats)
        cases.append(case)
        print(json.dumps({'trace':trace.name,'available_scores':len(trace.available),
                          'max_acf':{key:v['max_score'] for key,v in stats.items()}}),flush=True)
    result=dict(date='2026-11-11',interval_seconds=15,
                acf_class_sha256=hashlib.sha256(inspect.getsource(ACFDetector).encode()).hexdigest(),
                generator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                detector_module_sha256=hashlib.sha256(Path(inspect.getfile(ACFDetector)).read_bytes()).hexdigest(),
                profiles=PROFILES,source_manifest=manifest,cases=cases)
    write_json(output/'summary.json',result)
    summary_rows=[]
    for case in cases:
        for key,r in case['profiles'].items():
            summary_rows.append(dict(case=case['name'],profile=key,ready_points=r['ready_points'],
                                     max_score=r['max_score'],depth_at_peak=r['depth_at_peak'],max_depth=r['max_depth'],
                                     max_depth_seconds=r['max_depth_seconds'],peak_issued_at=r['peak_issued_at']))
    write_csv(output/'summary.csv',summary_rows)
    render_html(result,output)
    return result


def render_html(meta,output):
    body=['<h1>ACF поверх score других детекторов</h1>',
          '<p class="lead">Три приложенных примера • пять выходных рядов score • шаг 15 секунд • отдельный отчет</p>',
          '<div class="notice">На вход ACF подаются <b>численные score</b>, без пороговой бинаризации. '
          'Исходные TPS и latency сверху показаны для контекста. Расчет идет по реальному времени выдачи '
          'нижнего score, без знания будущих значений. Алгоритмы детекторов и прежние отчеты не менялись.</div>',
          '<p>Включены CUSUM 10/200 и 40/800 на оранжевом TPS, относительный Correlation score и '
          'CCF-only вариант из пунктира на скриншоте latency, накопленный Trend score Cassandra-1. '
          'Сохраненные выходы CUSUM, Correlation и Trend сверены с повторным запуском текущих классов. '
          'Данные происходят из Influx replay, ранее реконструированных по изображениям; экспертной разметки аварий нет.</p>',
          '<h2>Настройки ACF</h2>']
    body.append(table(['Профиль','Текущее окно','Фон','Лаги / горизонт','threshold','Первый результат после N готовых score'],[
        [key,f"{c['window']} точек / {c['window']/4:g} мин",f"{c['reference_window']} / {c['reference_window']/4:g} мин",
         f"{c['max_lag']} / {15*c['max_lag']} с",c['threshold'],c['reference_window']+c['window']]
        for key,c in PROFILES.items()]))
    body.extend(['<p>default и short — существующие настройки из отчета ACF. short_reference сокращает только фон '
                 'до 40 точек. compact — дополнительная конфигурация для короткой записи: окно 16, фон 32, '
                 '4 лага и threshold=2 вместо 3.5. Это проверка чувствительности; пороги не калибровались '
                 'на одинаковую частоту ложных тревог. Сравнение не является рейтингом точности.</p>',
                 '<h2>Прогрев и время выдачи</h2>',
                 '<p>Нулевые заглушки до lower.ready не поступают в ACF. После первого готового score ACF '
                 'сначала собирает собственный фон, затем отдельное текущее окно. Фон фиксируется по первым '
                 'reference_window доступным score; последующие наблюдения его не меняют. '
                 'В отчетных CSV до готовности ACF указаны пустые значения, на графике — разрыв, а не нулевой балл.</p>',
                 '<p>У Correlation допуск временного сдвига ±1 отсчет: точка score за 16:53:45 становится '
                 'доступна в 16:54:00. Использовано именно issued_at. Последний pending score не заменяется нулем. '
                 'Готовые предварительные оценки growing-window включены как реальные выдачи и помечены в CSV. '
                 'Основной latency score дает только 107 готовых точек: default и short не успевают прогреться.</p>',
                 '<h2>Результаты</h2>'])
    rows=[]
    for case in meta['cases']:
        for key,r in case['profiles'].items():
            score='не готов' if r['max_score'] is None else f"{r['max_score']:.5f}"
            depth='—' if r['max_depth'] is None else f"{r['max_depth']} / {r['max_depth_seconds']} с"
            rows.append([f'<a href="#{case["name"]}">{html.escape(case["title"])}</a>',key,r['ready_points'],
                         score,r['depth_at_peak'] if r['depth_at_peak'] is not None else '—',depth])
    body.append(table(['Вход','Профиль','Готовых оценок','Макс. балл ACF','Глубина при пике','Макс. глубина'],rows))
    latency=next(c for c in meta['cases'] if c['name']=='correlation-latency')
    p=latency['profiles']['compact']['at_input_score_peak']
    body.append('<div class="notice"><b>Почему latency почти не дает реакции.</b> '
                f'При максимальном входном score={p["score"]:.3f} '
                f'коэффициент на лаге 1 равен {p["current_lag1"]:.3f}, '
                f'в фоне {p["reference_lag1"]:.3f}; их разность '
                f'{abs(p["current_lag1"]-p["reference_lag1"]):.3f} ниже допуска {p["band_lag1"]:.3f}. '
                'Рост амплитуды score сам по себе не означает изменение его лаговой структуры. '
                'В точке максимума входного score балл ACF равен нулю; небольшой максимум ACF '
                'возникает в другой точке. Это результат compact-конфигурации с короткими окнами.</div>')
    if latency['profiles']['compact']['last_zero_energy']:
        tail=latency['profiles']['compact']['last_acf_score']
        body.append(f'<p>В конце latency входной score уже нулевой, но compact сохраняет балл {tail:.5f} '
                    'при <code>zero_energy=True</code>. У постоянного окна статистическая ACF не определена; '
                    'класс использует нулевой профиль, который немного отличается от ненулевого фонового. '
                    'Такой остаточный балл не означает продолжающееся физическое воздействие.</p>')
    body.extend(['<p><b>Интерпретация.</b> CUSUM с большой памятью сам создает длительные участки score; '
                 'ACF показывает структуру уже этого выхода. Это не независимая оценка продолжительности '
                 'физического воздействия в системе. Постоянный участок score=1 после заполнения окна '
                 'имеет нулевую энергию и при нулевом фоновом ACF перестает выделяться, хотя нижний score остается высоким.</p>',
                 '<p>Глубина — последний значимый лаг, не возраст события и не точная длительность последствий. '
                 'При достижении max_lag горизонт проверки исчерпан. Небольшой далекий эффект может давать '
                 'большую глубину при малом балле; оба числа нужно читать вместе. ACF инвариантен к масштабу '
                 'входа, поэтому высокая лаговая связь возможна и у малых нижних score.</p>',
                 '<p>Во всех основных графиках шкалы нижнего и верхнего score сохранены [0,1]. '
                 'Слабый отклик на коротком latency не увеличен визуальной перенормировкой. '
                 'Профили при максимуме показывают, какие лаги превысили эвристическую полосу. '
                 'Серая зона в панелях ACF — общий прогрев показанных профилей; более долгий '
                 'индивидуальный прогрев виден по отсутствию соответствующей линии.</p>'])
    for case in meta['cases']:
        name=case['name']
        body.extend([f'<h2 id="{name}">{html.escape(case["title"])}</h2>',
                     f'<p>{case["source_points"]} исходных точек, {case["lower_ready_scores"]} готовых score; '
                     f'первый доступный score: {case["first_lower_score_issued_at"]}. '
                     f'Предварительных: {case["lower_provisional_points"]}; pending: {case["lower_pending_points"]}.</p>',
                     f'<img src="{name}.png" loading="lazy" alt="{html.escape(case["title"])}">'])
        if not name.startswith('correlation'):
            body.append(f'<details><summary>Участок максимальной реакции крупнее</summary><img src="{name}-zoom.png" loading="lazy" alt="Увеличение"></details>')
        body.append(f'<details><summary>Лаговые профили в точках максимального балла</summary><img src="{name}-profiles.png" loading="lazy" alt="Лаговые профили"></details>')
        links=[]
        for key,r in case['profiles'].items():
            links.append(f'<a href="{name}-{key}.csv">{key}: все оценки</a>')
            if r['ready_points']:links.append(f'<a href="{name}-{key}-peak-lags.csv">{key}: лаги при пике</a>')
        body.append('<p class="small">'+' · '.join(links)+'</p>')
    body.extend(['<h2>Воспроизведение и проверка</h2>',
                 '<p><code>python -m tests.acf_detector_scores_report</code><br>'
                 '<code>python -m unittest tests.test_acf_detector_scores</code></p>',
                 '<p><a href="summary.json">Полная сводка JSON</a> · <a href="summary.csv">Сводка CSV</a> · '
                 '<a href="inputs/manifest.json">Происхождение и SHA256 входов</a> · '
                 '<a href="../../ACF.md">Описание ACF</a> · <a href="test-results.txt">Проверки</a>.</p>',
                 '<p class="small">ACF class SHA256: '+meta['acf_class_sha256']+'.</p>'])
    css='body{font:16px/1.6 system-ui;margin:0;background:#f5f7fa;color:#25334a}main{max-width:1250px;margin:auto;background:white;padding:40px}h1{font-size:31px;line-height:1.25}h2{margin-top:38px}.lead,.small{color:#64748b}.small{font-size:13px}.notice{background:#edf3ff;border-left:5px solid #2563eb;padding:18px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}th{background:#eef2f8}img{max-width:100%;height:auto}a{color:#245cb0}details{border:1px solid #ddd;padding:12px;margin:16px 0}summary{cursor:pointer;font-weight:600}'
    (output/'report.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>ACF над score детекторов</title><style>'+css+'</style><main>'+''.join(body)+'</main></html>',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'artifacts'/'acf-detector-scores')
    parser.add_argument('--fixtures',type=Path,default=FIXTURES)
    args=parser.parse_args();create_report(args.output,args.fixtures)
    print(args.output/'report.html',flush=True)


if __name__=='__main__':main()
