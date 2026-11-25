"""Independent per-host ACF runs for Grafana panel 2, at native five seconds."""
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import argparse,csv,hashlib,html,inspect,json
from pathlib import Path
import shutil

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from apps.detectors import ACFDetector

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=Path(__file__).with_name('fixtures')/'acf-dispatcher'
MSK=timezone(timedelta(hours=3))
CONFIGS={
    'short':dict(window=40,max_lag=10,reference_window=160,threshold=3.5,min_change=.1),
    'compact':dict(window=16,max_lag=4,reference_window=64,threshold=2.,min_change=.1),
}
COLORS={'host1':'#7E9B68','host4':'#9778B4','host6':'#C38489','host7':'#D8AE6D','host8':'#5E5867'}
LINES={'short':'#2563eb','compact':'#b86520'}
START_MARK='<!-- acf-dispatcher-focus:start -->'
END_MARK='<!-- acf-dispatcher-focus:end -->'


def stamp(value):return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(MSK)


def save_csv(path,rows):
    with path.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def save_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


@dataclass
class HostCase:
    host:str
    times:tuple
    values:np.ndarray
    focus_start:datetime
    focus_stop:datetime
    reference_start:datetime
    reference_stop:datetime

    @property
    def focus(self):return np.array([self.focus_start<=t<=self.focus_stop for t in self.times])

    @property
    def reference(self):return np.array([self.reference_start<=t<=self.reference_stop for t in self.times])


def load_cases(fixtures=FIXTURES):
    fixtures=Path(fixtures)
    meta=json.loads((fixtures/'metadata.json').read_text(encoding='utf-8'))
    for name,digest in meta['files'].items():
        if hashlib.sha256((fixtures/name).read_bytes()).hexdigest()!=digest:raise ValueError('Snapshot changed: '+name)
    with (fixtures/'dispatcher-all.csv').open(encoding='utf-8',newline='') as f:rows=list(csv.DictReader(f))
    groups={}
    for r in rows:groups.setdefault(r['host'],[]).append(r)
    if set(groups)!=set(COLORS):raise ValueError('Panel host identities changed; review new source before exporting')
    cases=[]
    for host,rows in sorted(groups.items()):
        times=tuple(stamp(r['timestamp_utc']) for r in rows)
        values=np.array([float(r['value_seconds']) for r in rows])
        if any((b-a).total_seconds()!=5 for a,b in zip(times,times[1:])):raise ValueError('Native grid changed')
        if not np.isfinite(values).all():raise ValueError('Nonfinite values')
        cases.append(HostCase(host,times,values,stamp(meta['focus_start_utc']),stamp(meta['focus_stop_utc']),
                              stamp(meta['baseline_start_utc']),stamp(meta['baseline_stop_utc'])))
    return cases,meta


def replay(case,config):
    """Collect known baseline chronologically; fit before any scored sample."""
    detector=ACFDetector(**config)
    reference=[];fitted=False;rows=[];snapshots=[]
    for i,(t,value) in enumerate(zip(case.times,case.values)):
        if t<case.reference_start:
            phase='before_reference'
        elif t<=case.reference_stop:
            reference.append(float(value));phase='reference'
            if t==case.reference_stop:
                detector.fit(reference);fitted=True
        elif fitted:
            detector.update(float(value));phase='scoring' if detector.ready else 'local_warmup'
        else:
            raise ValueError('Missing complete baseline; no invented history')
        r=detector.result
        rows.append(dict(index=i,timestamp_utc=t.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
                         timestamp_msk=t.isoformat(),value_seconds=float(value),
                         in_focus=case.focus_start<=t<=case.focus_stop,phase=phase,reference_count=len(reference),
                         ready=r.ready,score=r.score if r.ready else None,depth=r.depth if r.ready else None,
                         depth_seconds=5*r.depth if r.ready else None,
                         horizon_reached=r.horizon_reached if r.ready else None,
                         zero_energy=r.zero_energy if r.ready else None))
        snapshots.append(r)
    return rows,snapshots


def summarize(case,config,rows,snapshots):
    focus=[r for r in rows if r['in_focus'] and r['ready']]
    peak=max(focus,key=lambda r:r['score']) if focus else None
    ready=[r for r in rows if r['ready']]
    return dict(config=config,actual_reference_points=int(case.reference.sum()),
                focus_points=int(case.focus.sum()),ready_focus_points=len(focus),
                first_ready_msk=ready[0]['timestamp_msk'] if ready else None,
                max_score=peak['score'] if peak else None,peak_index=peak['index'] if peak else None,
                peak_msk=peak['timestamp_msk'] if peak else None,depth_at_peak=peak['depth'] if peak else None,
                max_depth=max((r['depth'] for r in focus),default=None),
                max_depth_seconds=max((r['depth_seconds'] for r in focus),default=None),
                nonzero_focus_points=sum(r['score']>0 for r in focus),
                horizon_reached_points=sum(r['horizon_reached'] for r in focus))


def render_host(case,results,stats,output,context=False):
    fig,axes=plt.subplots(3,1,figsize=(13,9.5),sharex=True,layout='constrained',
                          gridspec_kw={'height_ratios':[2.3,1.2,1.2]})
    axes[0].plot(case.times,case.values,color=COLORS[case.host],lw=1.2)
    axes[0].set_title(f'{case.host}: задержка диспетчера → ACF; шаг 5 секунд',loc='left',fontsize=15,fontweight='bold')
    axes[0].set_ylabel('Исходный ряд, с')
    for key,cfg in CONFIGS.items():
        rows,_=results[key]
        label=f'{key}: окно {cfg["window"]}, лаг {cfg["max_lag"]}, threshold {cfg["threshold"]}'
        axes[1].plot(case.times,[r['score'] if r['ready'] else np.nan for r in rows],color=LINES[key],label=label)
        axes[2].plot(case.times,[r['depth_seconds'] if r['ready'] else np.nan for r in rows],
                     color=LINES[key],drawstyle='steps-post',label=label)
    axes[1].set_ylim(-.01,1.02);axes[1].set_ylabel('Балл ACF [0,1]');axes[1].legend(fontsize=8,loc='upper left')
    axes[2].set_ylim(-1,55);axes[2].set_ylabel('Глубина, секунды');axes[2].set_xlabel('5 октября 2026, МСК')
    start,stop=(case.times[0],case.times[-1]) if context else (case.focus_start,case.focus_stop)
    common_first=min(stamp(s['first_ready_msk']) for s in stats.values() if s['first_ready_msk'])
    for ax in axes:
        ax.set_xlim(start,stop);ax.grid(alpha=.18)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK,minticks=5,maxticks=9))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S',tz=MSK))
        ax.axvspan(case.reference_start,case.reference_stop,color='#2563eb',alpha=.08)
        for bound in (case.focus_start,case.focus_stop):ax.axvline(bound,color='#64748b',ls=':',lw=.8)
    for ax in axes[1:]:ax.axvspan(case.reference_stop,common_first,color='#64748b',alpha=.08)
    if context:
        for ax in axes:ax.axvspan(case.focus_start,case.focus_stop,color='#e7b230',alpha=.08)
    fig.savefig(output/(case.host+('-context' if context else '-focus')+'.png'),dpi=130);plt.close(fig)


def render_lags(case,results,stats,output):
    fig,axes=plt.subplots(1,2,figsize=(12,4),sharey=True,layout='constrained')
    for ax,(key,cfg) in zip(axes,CONFIGS.items()):
        r=results[key][1][stats[key]['peak_index']]
        lag=np.arange(1,cfg['max_lag']+1)
        ref=np.array(r.reference_acf[1:]);band=np.array(r.thresholds[1:])
        ax.fill_between(lag,np.maximum(-1,ref-band),np.minimum(1,ref+band),color='#64748b',alpha=.15,label='Эвристическая полоса')
        ax.plot(lag,r.acf[1:],color=LINES[key],label='ACF текущего окна')
        ax.plot(lag,ref,color='#cf8c20',label='ACF фона')
        if r.significant_lags:ax.scatter(r.significant_lags,[r.acf[k] for k in r.significant_lags],color='#dc2626',s=20)
        ax.set_title(f'{key}: максимум балла {r.score:.5f}',fontsize=11)
        ax.set_ylim(-1.05,1.05);ax.grid(alpha=.15);ax.set_xlabel('Лаг, точки × 5 секунд');ax.legend(fontsize=8)
    axes[0].set_ylabel('ACF');fig.savefig(output/(case.host+'-lags.png'),dpi=130);plt.close(fig)


def header(title):
    css='body{font:16px/1.6 system-ui;margin:0;background:#f5f7fa;color:#25334a}main{max-width:1200px;margin:auto;background:white;padding:38px}h1{font-size:29px;line-height:1.25}h2{margin-top:35px}img{max-width:100%;height:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:9px;border-bottom:1px solid #ddd;text-align:left}a{color:#245cb0}.note{padding:16px;background:#edf3ff;border-left:4px solid #2563eb}details{border:1px solid #ddd;padding:12px;margin:18px 0}summary{cursor:pointer}code{font-size:.9em}'
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>'+title+'</title><style>'+css+'</style><main>'


def table(cases,link_prefix=''):
    text='<table><tr><th>Отдельный ряд</th><th>Профиль</th><th>Max ACF в интервале ссылки</th><th>Глубина при пике, лаги</th><th>Макс. глубина, с</th></tr>'
    for case in cases:
        for key,m in case['variants'].items():
            text+=f'<tr><td><a href="{link_prefix}{case["host"]}.html">{case["host"]}</a></td><td>{key}</td><td>{m["max_score"]:.5f}</td><td>{m["depth_at_peak"]}</td><td>{m["max_depth_seconds"]}</td></tr>'
    return text+'</table>'


def method_text(meta):
    return ('<p>Панель 2: <code>service_dispatcher_delay</code>, поле value, единицы — секунды. '
            'Пять host обработаны <b>отдельными экземплярами ACFDetector</b>; максимумы и средние разных рядов не объединяются. '
            'Сохранен исходный шаг 5 секунд, значения поточечно совпадают с ответом Grafana.</p>'
            '<p>Точный интервал ссылки: 05.10.2026 <b>18:17:37.276–18:29:23.827 МСК</b>. '
            'На пятисекундной сетке это 18:17:40–18:29:20, по 141 точке на host. '
            'Показатели в таблице рассчитаны только по этому интервалу; полный исходник 18:10–18:45 доступен для контекста.</p>'
            '<p>Для каждого host фон взят из заранее заданного в replay участка <b>18:15:00–18:19:15</b>: '
            '52 собственные точки 0.010–0.020 с. Внутри выбранного интервала его первые 20 точек еще относятся к обучению. '
            'Модель фиксируется в конце этого участка; оценка начинается после отдельного полного текущего окна, '
            'без его пересечения с фоном. Будущие точки не участвуют в ранее выданных результатах.</p>'
            '<p><b>short:</b> window=40 (3 мин 20 с), max_lag=10 (50 с), reference_window=160 (верхний предел), '
            'threshold=3.5. Реально доступный фон — 52 точки. Первый готовый результат в 18:22:35, '
            '82 готовых результата внутри ссылки. <b>compact:</b> window=16 (80 с), max_lag=4 (20 с), '
            'reference_window=64, threshold=2; тот же фон 52 точки. Первый результат в 18:20:35, '
            '106 готовых результатов. min_change=0.1 в обоих случаях.</p>'
            '<p>compact — дополнительная проверка чувствительности. Пороги не приводились к общей частоте ложных тревог. '
            'Конфигурация default 256/512 не применена: 52 точки известного фона недостаточно для fit(window=256), '
            'а полный replay из 421 точки короче автоматического прогрева 512+256. История не дополнена искусственно.</p>'
            '<p>Синим выделен фон, серым — общий прогрев после него. До ready на графиках разрывы, в CSV пустые '
            'поля. Глубина — последний значимый лаг × 5 секунд, а не возраст инцидента или время восстановления. '
            'ACF оценивает изменение структуры лаговых связей, а не амплитуду задержки. Вход — ранее восстановленный '
            'по изображению replay; фон 10–20 мс был задан пользователем, экспертных меток аварии нет.</p>')


def create_report(output,fixtures=FIXTURES):
    output=Path(output);output.mkdir(exist_ok=True,parents=True)
    cases,meta=load_cases(fixtures)
    archive=output/'inputs';archive.mkdir(exist_ok=True)
    for p in Path(fixtures).iterdir():
        if p.is_file():shutil.copy2(p,archive/p.name)
    summary=[]
    for case in cases:
        results={};stats={}
        for key,cfg in CONFIGS.items():
            rows,snapshots=replay(case,cfg);results[key]=(rows,snapshots)
            stats[key]=summarize(case,cfg,rows,snapshots)
            save_csv(output/f'{case.host}-{key}.csv',rows)
            k=stats[key]['peak_index'];r=snapshots[k]
            save_csv(output/f'{case.host}-{key}-peak-lags.csv',[
                dict(lag=i,lag_seconds=i*5,acf=r.acf[i],reference_acf=r.reference_acf[i],
                     difference=r.lag_changes[i],band=r.thresholds[i],significant=i in r.significant_lags)
                for i in range(cfg['max_lag']+1)])
        save_csv(output/f'{case.host}-reference.csv',[
            dict(timestamp_msk=t.isoformat(),value_seconds=float(v)) for t,v,use in zip(case.times,case.values,case.reference) if use])
        render_host(case,results,stats,output)
        render_host(case,results,stats,output,context=True)
        render_lags(case,results,stats,output)
        item=dict(host=case.host,points=len(case.values),focus_points=int(case.focus.sum()),
                  values_sha256=hashlib.sha256(case.values.astype('<f8').tobytes()).hexdigest(),variants=stats)
        summary.append(item)
        links=' · '.join(f'<a href="{case.host}-{k}.csv">{k}: все точки</a> · <a href="{case.host}-{k}-peak-lags.csv">лаги при пике</a>' for k in CONFIGS)
        body=header('ACF: '+case.host)+f'<h1>ACF отдельно: {case.host}</h1><p><a href="index.html">Пять отдельных отчетов</a> · <a href="../report.html#dispatcher-focus">Основной отчет ACF</a></p>'
        body+=table([item])+f'<img src="{case.host}-focus.png" alt="{case.host}: исходный ряд, балл и глубина ACF">'
        body+='<p>'+links+f' · <a href="{case.host}-reference.csv">52 точки фона</a></p>'
        body+=f'<h2>Лаговые профили в точках максимального балла</h2><img src="{case.host}-lags.png" alt="Лаги {case.host}">'
        body+=f'<details><summary>Полный исходный replay с выделенным интервалом ссылки</summary><img src="{case.host}-context.png" alt="Контекст {case.host}"></details>'
        body+='<h2>Методика</h2>'+method_text(meta)+'</main></html>'
        (output/f'{case.host}.html').write_text(body,encoding='utf-8')
        print(json.dumps(dict(host=case.host,max_scores={k:s['max_score'] for k,s in stats.items()})),flush=True)
    result=dict(date='2026-11-13',source=meta,hosts=summary,settings=CONFIGS,
                acf_class_sha256=hashlib.sha256(inspect.getsource(ACFDetector).encode()).hexdigest(),
                detector_module_sha256=hashlib.sha256(Path(inspect.getfile(ACFDetector)).read_bytes()).hexdigest(),
                generator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    save_json(output/'summary.json',result)
    save_csv(output/'summary.csv',[dict(host=h['host'],profile=k,max_score=s['max_score'],max_depth=s['max_depth'],
             max_depth_seconds=s['max_depth_seconds'],first_ready_msk=s['first_ready_msk'],peak_msk=s['peak_msk'],
             ready_focus_points=s['ready_focus_points']) for h in summary for k,s in h['variants'].items()])
    intro=header('ACF: задержка диспетчера по отдельным host')+'<h1>ACF: пять рядов диспетчера по отдельности</h1>'
    intro+='<p><a href="../report.html#dispatcher-focus">Основной отчет ACF</a> · <a href="summary.json">Сводка JSON</a> · <a href="summary.csv">Сводка CSV</a> · <a href="test-results.txt">Проверки</a></p>'
    intro+=table(summary)+'<h2>Методика и происхождение</h2>'+method_text(meta)+'</main></html>'
    (output/'index.html').write_text(intro,encoding='utf-8')
    appendix=START_MARK+'\n<section id="dispatcher-focus"><h2>13 ноября: задержка диспетчера, панель 2, отдельно по host</h2>'
    appendix+='<p>Интервал ссылки: 05.10.2026 18:17:37.276–18:29:23.827 МСК. Пять независимых рядов '
    appendix+='с шагом 5 секунд, по 141 исходной точке в окне. Значения сверены с Grafana; фон каждого host '
    appendix+='обучен отдельно по 52 точкам 18:15–18:19:15. Ни максимум, ни среднее разных host не используются.</p>'
    appendix+=table(summary,'dispatcher/')
    appendix+='<p><a href="dispatcher/index.html">Методика и пять отдельных HTML-отчетов</a> · '
    appendix+='<a href="dispatcher/summary.json">JSON</a> · <a href="dispatcher/test-results.txt">Проверки</a>.</p>'
    appendix+='<p>short: 40 точек / 10 лагов / threshold=3.5; compact: 16 / 4 / threshold=2. '
    appendix+='Балл на этом фрагменте невелик: в short около 0.006–0.007, в compact около 0.056–0.060. '
    appendix+='ACF сравнивает лаговую структуру с фоном; сильный рост амплитуды задержки сам по себе '
    appendix+='не гарантирует большой балл. До ready результаты не показаны как измеренные нули.</p></section>\n'+END_MARK
    (output/'appendix.html').write_text(appendix,encoding='utf-8')
    return result


def merge_appendix(document,appendix):
    """Byte-preserving, idempotent insertion/replacement of this section only."""
    start,end=START_MARK.encode(),END_MARK.encode()
    a=document.find(start);b=document.find(end)
    if a>=0:
        if b<a:raise ValueError('Incomplete existing dispatcher section')
        return document[:a]+appendix+document[b+len(end):]
    if b>=0:raise ValueError('Incomplete existing dispatcher section')
    point=document.lower().rfind(b'</body>')
    if point<0:point=document.lower().rfind(b'</html>')
    if point<0:point=len(document)
    return document[:point]+b'\n'+appendix+b'\n'+document[point:]


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'artifacts/acf/dispatcher')
    parser.add_argument('--fixtures',type=Path,default=FIXTURES)
    parser.add_argument('--append-main',type=Path)
    args=parser.parse_args();create_report(args.output,args.fixtures)
    if args.append_main:
        args.append_main.write_bytes(merge_appendix(args.append_main.read_bytes(),(args.output/'appendix.html').read_bytes()))
    print(args.output/'index.html',flush=True)
