"""CUSUM response on frozen corrected InfluxDB Scenario and Cassandra snapshots."""

from datetime import timezone
import hashlib
import html
import inspect
import json
from pathlib import Path
import re
import shutil

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from apps.detectors import CusumDetector
from tests.acf_replay_scenarios import replay_scenarios
from tests.trend_replay_appendices import FIXTURES,SNAPSHOT,MSK,save_csv
from tests.scenario_influx import STEM,COLORS,load_snapshot


SETTINGS={
    'default':{'window':10,'inertia_window':200},
    'wide':{'window':40,'inertia_window':800},
}
MARKER='<!-- cusum-latest-inputs -->'


def replay_case(case,settings):
    detector=CusumDetector(**settings)
    rows=[]
    for i,(value,stamp) in enumerate(zip(case.values,case.timestamps)):
        score=detector.update(float(value))
        rows.append({'index':i,'timestamp_msk':stamp.astimezone(MSK).isoformat(),
                     'timestamp_utc':stamp.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
                     'seconds':i*15,'value':float(value),'ready':detector.ready,'score':score,
                     'above_0_5':detector.ready and score>=.5,
                     'noise_std':detector.noise_std,'background_level':detector.background_level,
                     'inertia_samples':len(detector._inertia)})
    return rows


def summarize(case,rows,settings):
    detector=CusumDetector(**settings)
    config={name:getattr(detector,name) for name in ('window','inertia_window','reference_window',
            'min_reference_samples','threshold','drift','relative_threshold','relative_drift','noise_floor','update_sigma')}
    ready=[r for r in rows if r['ready']]
    positive=[r for r in ready if r['score']>0]
    high=[r for r in ready if r['above_0_5']]
    runs=[]
    for r in high:
        if not runs or r['index']>runs[-1]['end_index']+1:
            runs.append({'start_index':r['index'],'end_index':r['index'],'start_msk':r['timestamp_msk'],
                         'end_msk':r['timestamp_msk'],'max_score':r['score']})
        else:
            runs[-1].update(end_index=r['index'],end_msk=r['timestamp_msk'],max_score=max(runs[-1]['max_score'],r['score']))
    phases=[]
    for phase in case.phases:
        part=[r for r in rows[phase['start']:phase['end']] if r['ready']]
        phases.append({**phase,'ready_samples':len(part),'max_score':max((r['score'] for r in part),default=None),
                       'samples_above_0_5':sum(r['above_0_5'] for r in part)})
    return {'config':config,'ready_samples':len(ready),'max_score':max(r['score'] for r in rows),
            'nonzero_samples':len(positive),'samples_above_0_5':len(high),'runs_above_0_5':runs,
            'first_ready_index':ready[0]['index'] if ready else None,
            'first_nonzero_msk':positive[0]['timestamp_msk'] if positive else None,
            'first_above_0_5_msk':high[0]['timestamp_msk'] if high else None,
            'max_inertia_samples':max(r['inertia_samples'] for r in rows),'phases':phases}


def render_case(output,case,results):
    t=mdates.date2num(case.timestamps)
    fig,axes=plt.subplots(5,1,figsize=(14,13),layout='constrained',gridspec_kw={'height_ratios':[3,1.6,1.4,1.4,2]})
    fig.suptitle('CUSUM: '+case.label+'\nШаг 15 с; окно/память 10/200 и 40/800',fontsize=12)
    axes[0].set_facecolor('#181d24')
    trace_color=COLORS.get(case.name.removeprefix('influx-scenario-'),'#f59e0b')
    axes[0].plot(t,case.values,color=trace_color,lw=.9,drawstyle='default' if case.phases else 'steps-post')
    axes[0].set_ylabel('TPS' if case.phases else 'мс')
    for key,color,label in [('default','#dc2626','Окно 10, память 200'),('wide','#2563eb','Окно 40, память 800')]:
        rows=results[key]
        axes[1].plot(t,[r['score'] if r['ready'] else np.nan for r in rows],color=color,lw=.9,label=label)
        axes[2].plot(t,[r['noise_std'] if r['noise_std'] is not None else np.nan for r in rows],color=color,lw=.7,label=label)
        axes[3].plot(t,[r['background_level'] if r['background_level'] is not None else np.nan for r in rows],color=color,lw=.7,label=label)
    axes[1].axhline(.5,color='#64748b',ls='--',lw=.8,label='Справочная граница 0.5')
    axes[1].set_ylim(-.02,1.03);axes[1].set_ylabel('Score [0, 1]');axes[1].legend(fontsize=8,ncol=3)
    axes[2].set_yscale('symlog',linthresh=1e-8);axes[2].set_ylabel('Шум разностей\nsymlog')
    axes[3].set_ylabel('Фоновый уровень')
    for ax in axes[:4]:
        ax.set_xlim(t[0],t[-1])
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK,minticks=4,maxticks=8))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m\n%H:%M',tz=MSK))
        ax.grid(alpha=.2)
    zoom=case.zoom_index if case.zoom_index is not None else int(np.argmax(abs(np.diff(case.values))))+1
    a=max(0,zoom-8);b=min(len(t),zoom+9)
    axes[4].plot(t[a:b],case.values[a:b],color=trace_color,marker='o',ms=3,drawstyle='default' if case.phases else 'steps-post')
    axes[4].scatter(t[zoom],case.values[zoom],s=35,color='#dc2626',zorder=5)
    axes[4].xaxis.set_major_locator(mdates.SecondLocator(bysecond=(0,30),tz=MSK))
    axes[4].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S',tz=MSK))
    axes[4].set_title('Исходные точки крупно: один отсчёт через 15 секунд',fontsize=10)
    axes[4].set_xlabel('Europe/Moscow (UTC+03:00)');axes[4].grid(alpha=.2)
    image='recent-'+case.name+'.png'
    fig.savefig(output/image,dpi=110);plt.close(fig)
    return image


def create_appendix(output):
    output.mkdir(parents=True,exist_ok=True)
    cases=[];data={}
    for case in replay_scenarios():
        results={};variants={}
        for key,settings in SETTINGS.items():
            rows=replay_case(case,settings);results[key]=rows
            stem='recent-'+case.name+'-'+key
            save_csv(output/(stem+'.csv'),rows)
            variants[key]={**summarize(case,rows,settings),'stem':stem}
        image=render_case(output,case,results)
        cases.append({'scenario':'recent-'+case.name,'report_group':'latest-inputs','name':case.name,
                      'label':case.label,'source':case.source,'points':len(case.values),'image':image,
                      'data_sha256':hashlib.sha256(np.asarray(case.values,dtype='<f8').tobytes()).hexdigest(),
                      'variants':variants})
        data[case.name]=(case,results)
    fig,axes=plt.subplots(3,1,figsize=(14,10),layout='constrained',sharex=True)
    for name,(case,results) in data.items():
        if name.startswith('influx-scenario-'):continue
        host=name.removeprefix('influx-')
        axes[0].plot(case.timestamps,case.values,lw=.6,drawstyle='steps-post',label=host)
        for i,key in enumerate(SETTINGS,1):
            axes[i].plot(case.timestamps,[r['score'] if r['ready'] else np.nan for r in results[key]],lw=.7,label=host)
    axes[0].set_title('Cassandra из InfluxDB: те же исходные значения и реакция CUSUM')
    axes[0].set_ylabel('мс');axes[1].set_ylabel('Score 10/200');axes[2].set_ylabel('Score 40/800')
    for ax in axes[1:]:ax.set_ylim(-.02,1.03);ax.axhline(.5,color='#64748b',ls='--',lw=.7)
    for ax in axes:ax.grid(alpha=.2);ax.legend(fontsize=9,ncol=4)
    axes[2].xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK,minticks=4,maxticks=8))
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter('%d.%m\n%H:%M',tz=MSK))
    axes[2].set_xlabel('Europe/Moscow (UTC+03:00)')
    fig.savefig(output/'recent-influx-overview.png',dpi=110);plt.close(fig)
    for prefix in (STEM,SNAPSHOT):
        for path in FIXTURES.glob(prefix+'*'):shutil.copy2(path,output/path.name)
    result={'date':'2026-11-11','sample_interval_seconds':15,'series_count':len(cases),'input_points':sum(c['points'] for c in cases),
            'variant_count':len(cases)*len(SETTINGS),'cases':cases,
            'cusum_class_sha256':hashlib.sha256(inspect.getsource(CusumDetector).encode()).hexdigest(),
            'generator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'scenario_source':load_snapshot()[1],
            'source_data':'Same frozen corrected InfluxDB Scenario and Cassandra snapshots as ACF; offline replay'}
    return result


def render_appendix(meta):
    table=''.join(f'<tr><td><a href="#recent-{c["name"]}">{html.escape(c["label"])}</a></td><td>{key}</td>'
                  f'<td>{v["max_score"]:.5f}</td><td>{v["samples_above_0_5"]}/{v["ready_samples"]}</td>'
                  f'<td>{len(v["runs_above_0_5"])}</td><td>{v["first_above_0_5_msk"] or "—"}</td></tr>'
                  for c in meta['cases'] for key,v in c['variants'].items())
    check=meta.get('test_run')
    test=(f'<p>Проверки CUSUM и новых наборов: {check["tests"]} тестов, пропущено {check["skipped"]}. '
          '<a href="recent-test-results.txt">Журнал</a>.</p>' if check else '')
    content=f'''<style>.cusum-replay table{{width:100%;border-collapse:collapse;font-size:13px}}
.cusum-replay td,.cusum-replay th{{padding:6px;border-bottom:1px solid #ddd;text-align:left}}</style>
<article class="cusum-replay" id="latest-inputs"><h2>Обновлено 11 ноября: Сценарий из InfluxDB, 17–18 октября</h2>
<p>{meta['input_points']:,} исходных точек с шагом 15 секунд: четыре TPS-ряда исправленного «Сценария»
и четыре host Cassandra из сохранённой InfluxDB. Данные совпадают с отчётом ACF. Алгоритм CUSUM используется без изменений.</p>
<p>default: window=10, inertia_window=200 (≈2.5 и 50 минут); wide: 40/800 (≈10 и 200 минут).
Остальные параметры по умолчанию: threshold=5, drift=3, reference_window=100, min_reference_samples=20,
relative_threshold=0.5, relative_drift=0.01, update_sigma=8, noise_floor=1e-9. Фон обучается в начале каждого отдельного ряда,
после чего допускается его обычное причинное обновление. Первый готовый индекс — 29 и 59 соответственно.</p>
<p>На графиках показаны score, фоновый шум разностей и фоновый уровень; пропуски до ready не означают измеренный ноль.
Граница 0.5 приведена для сравнения с прежними разделами отчёта. Последовательные точки score≥0.5 объединены в серии;
это не число независимых инцидентов и не отдельное подтверждение начала тренда.</p>
<p>У оранжевого ряда начальный участок равен нулю, поэтому sigma начинается с noise_floor. После скачка чувствительность
и относительная нормировка зависят от допуска значений в фон. Это видно в двух нижних диагностических рядах.
Score — инженерная выраженность двухстороннего движения с инерцией, не вероятность.</p>{test}
<table><tr><th>Ряд</th><th>Настройки</th><th>Max score</th><th>Точек ≥0.5 / готовых</th><th>Серий ≥0.5</th><th>Первое ≥0.5 MSK</th></tr>{table}</table>
<p><a href="recent-summary.json">Сводка JSON</a> · <a href="recent-summary.csv">Сводка CSV</a></p></article>'''
    for c in meta['cases']:
        if c['name'].startswith('influx-scenario-'):
            note=('3956 точек из InfluxDB: screenshot_step_replay, поле value, тег trace. '
                  '17 октября 2026 14:23:30 — 18 октября 06:52:15, Москва; TPS. '
                  'Снимок сверён с Grafana. Ширина пиков и откатов, наклонные участки и все промежуточные отсчёты сохранены. '
                  'Семь оранжевых откатов ниже верхнего плато занимают 5–14 отсчётов. '
                  'Происхождение: исправленная реконструкция по изображениям, не исходная телеметрия. '
                  'Начальный участок нулевой, поэтому score нужно оценивать вместе с шумом и фоновым уровнем.')
            extra=f'<img src="{STEM}-comparison.png" alt="Картинка пользователя и исправленный ряд">' if c['name']=='influx-scenario-orange' else ''
        else:
            note=('5761 точка Cassandra, 1 октября 2026 12:30 — 2 октября 2026 12:30 MSK, обе границы включены. '
                  'Снимок ранее поточечно сверён через прямой запрос InfluxDB и источник Grafana. В compose это replay, '
                  'реконструированный со скриншота, без достоверного эталона берстов. Значения не усредняются и не прореживаются.')
            extra=''
        links=''.join(f'<p>{key}: <a href="{v["stem"]}.csv">Все точки, ready, score, шум, уровень и размер памяти</a></p>'
                       for key,v in c['variants'].items())
        phases=''.join(f'<tr><td>{html.escape(p["label"])}</td><td>{key}</td><td>{p["max_score"]:.5f}</td>'
                       f'<td>{p["samples_above_0_5"]}/{p["ready_samples"]}</td></tr>' for key,v in c['variants'].items() for p in v['phases'])
        phase_table=(f'<table><tr><th>Фаза</th><th>Настройки</th><th>Max score</th><th>≥0.5 / готовых</th></tr>{phases}</table>' if phases else '')
        content+=f'<article class="cusum-replay" id="recent-{c["name"]}"><h2>{html.escape(c["label"])}</h2><p>{note}</p>{extra}'
        content+=f'<img src="{c["image"]}" alt="Исходный ряд и реакция CUSUM">{phase_table}{links}</article>'
        if c['name']=='influx-scenario-blue':
            content+=f'''<article id="recent-influx"><h2>Четыре ряда InfluxDB: общий вид</h2>
<img src="recent-influx-overview.png" alt="Cassandra и CUSUM score">
<p><a href="{SNAPSHOT}.csv">Все исходные точки</a> · <a href="{SNAPSHOT}-query.flux">Flux-запрос</a> ·
<a href="{SNAPSHOT}.json">Проверка источника и SHA256</a></p></article>'''
    content+=f'<p><a href="{STEM}.csv">Все четыре TPS-ряда из InfluxDB</a> · <a href="{STEM}-query.flux">Flux</a> · <a href="{STEM}.json">Происхождение и SHA256</a></p>'
    return content


def append_recent(output,test_log=None):
    output=Path(output)
    summary_path=output/'summary.json';html_path=output/'report.html'
    base=json.loads(summary_path.read_text(encoding='utf-8'))
    base=[row for row in base if row.get('report_group')!='latest-inputs']
    meta=create_appendix(output)
    meta['base_scenarios']=len(base)
    if test_log is not None:
        text=Path(test_log).read_text(encoding='utf-8')
        match=re.search(r'Ran (\d+) tests? in ([\d.]+)s',text)
        if not match or not re.search(r'^OK(?: \(skipped=\d+\))?\s*$',text,re.M) or 'FAILED (' in text:
            raise ValueError('Expected a successful completed unittest log')
        skip=re.search(r'OK \(skipped=(\d+)\)',text)
        meta['test_run']={'tests':int(match[1]),'seconds':float(match[2]),'skipped':int(skip[1]) if skip else 0}
        if Path(test_log).resolve()!=(output/'recent-test-results.txt').resolve():shutil.copy2(test_log,output/'recent-test-results.txt')
    (output/'recent-summary.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    save_csv(output/'recent-summary.csv',[{'name':c['name'],'config':key,'points':c['points'],
             **{k:v[k] for k in ('max_score','ready_samples','nonzero_samples','samples_above_0_5','first_above_0_5_msk')}}
             for c in meta['cases'] for key,v in c['variants'].items()])
    document=html_path.read_text(encoding='utf-8').split(MARKER)[0].removesuffix('</html>')
    document=re.sub(r'(<a href="#latest-inputs">).*?(</a>)',r'\1Сценарий из InfluxDB, 17–18 октября, и Cassandra\2',document)
    if 'href="#latest-inputs"' not in document:
        document=document.replace('</h1>','</h1><p><a href="#latest-inputs">Сценарий из InfluxDB, 17–18 октября, и Cassandra</a></p>',1)
    document+=MARKER+render_appendix(meta)+'</html>'
    html_path.write_text(document,encoding='utf-8')
    summary_path.write_text(json.dumps(base+meta['cases'],ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return meta
