"""ACF diagnostics on frozen InfluxDB Scenario and Cassandra snapshots."""

from dataclasses import asdict
from datetime import timezone
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
from tests.acf_replay_scenarios import SETTINGS, replay_scenarios
from tests.trend_replay_appendices import FIXTURES, SNAPSHOT, MSK, save_csv
from tests.scenario_influx import STEM, COLORS, load_snapshot


def replay_case(case, settings):
    """Automatic prefix training: no fit of future values or reference overlap."""
    detector=ACFDetector(**settings)
    rows=[]
    peak=None
    peak_index=None
    for i,(value,stamp) in enumerate(zip(case.values,case.timestamps)):
        score=detector.update(float(value))
        result=detector.result
        phase=('reference' if i<detector.reference_window else 'local_warmup' if not result.ready else 'scored')
        rows.append({'index':i,'timestamp_utc':stamp.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
                     'timestamp_msk':stamp.astimezone(MSK).isoformat(),'seconds':i*15,'value':float(value),
                     'phase':phase,'reference_ready':detector.reference_ready,'ready':result.ready,'score':score,
                     'depth':result.depth,'depth_seconds':result.depth*15,'lag_mass':result.lag_mass,
                     'horizon_reached':result.horizon_reached,'significant_lags':' '.join(map(str,result.significant_lags)),
                     'zero_energy':result.zero_energy,'reference_zero_energy':result.reference_zero_energy})
        if result.ready and (peak is None or result.score>peak.score):
            peak=result
            peak_index=i
    return rows,peak,peak_index


def summarize(case, rows, peak, peak_index, settings):
    ready=[r for r in rows if r['ready']]
    nonzero=[r for r in ready if r['score']>0]
    phases=[]
    for phase in case.phases:
        part=rows[phase['start']:phase['end']]
        measured=[r for r in part if r['ready']]
        phases.append({**phase,'ready_samples':len(measured),'nonzero_samples':sum(r['score']>0 for r in measured),
                       'max_score':max((r['score'] for r in measured),default=None),
                       'max_depth':max((r['depth'] for r in measured),default=None)})
    return {'config':settings,'points':len(rows),'reference_samples':settings['reference_window'],
            'reference_end_msk':rows[settings['reference_window']-1]['timestamp_msk'],
            'first_ready_index':ready[0]['index'] if ready else None,
            'first_ready_msk':ready[0]['timestamp_msk'] if ready else None,
            'ready_samples':len(ready),'nonzero_samples':len(nonzero),
            'nonzero_fraction_ready':len(nonzero)/len(ready) if ready else None,
            'first_nonzero_index':nonzero[0]['index'] if nonzero else None,
            'first_nonzero_msk':nonzero[0]['timestamp_msk'] if nonzero else None,
            'max_score':peak.score if peak else None,'peak_index':peak_index,
            'peak_msk':rows[peak_index]['timestamp_msk'] if peak_index is not None else None,
            'depth_at_peak_score':peak.depth if peak else None,
            'max_depth':max((r['depth'] for r in ready),default=None),
            'peak':asdict(peak) if peak else None,'phases':phases}


def render_case(output,case,results):
    times=mdates.date2num(case.timestamps)
    fig,axes=plt.subplots(5,1,figsize=(14,14),layout='constrained',gridspec_kw={'height_ratios':[3,1.6,1.4,2.2,2]})
    fig.suptitle('ACFDetector: '+case.label+'\nШаг 15 секунд; 256/32 с фоном 512 и 40/10 с фоном 160',fontsize=12)
    axes[0].set_facecolor('#181d24')
    trace_color=COLORS.get(case.name.removeprefix('influx-scenario-'),'#f59e0b')
    axes[0].plot(times,case.values,color=trace_color,lw=.9,drawstyle='default' if case.phases else 'steps-post',label='Точки из InfluxDB')
    axes[0].set_ylabel('TPS' if case.phases else 'мс')
    default=results['default'][0]
    ref_end=SETTINGS['default']['reference_window']-1
    ready=next(i for i,r in enumerate(default) if r['ready'])
    for ax in axes[:3]:
        ax.axvspan(times[0],times[ref_end],color='#94a3b8',alpha=.15)
        ax.axvspan(times[ref_end],times[ready],color='#f59e0b',alpha=.09)
        ax.set_xlim(times[0],times[-1])
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK,minticks=4,maxticks=8))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m\n%H:%M',tz=MSK))
        ax.grid(alpha=.2)
    for key,color,label in [('default','#dc2626','256/32, фон 512'),('short','#2563eb','40/10, фон 160')]:
        rows,peak,_=results[key]
        axes[1].plot(times,[r['score'] if r['ready'] else np.nan for r in rows],color=color,lw=1,label=label)
        axes[2].step(times,[r['depth_seconds'] if r['ready'] else np.nan for r in rows],where='post',color=color,lw=.8,label=label)
    axes[1].set_ylabel('Score [0, 1]')
    axes[1].set_ylim(-.003,max(.04,max(p[1].score for p in results.values())*1.12))
    axes[2].set_ylabel('Глубина связи, с')
    axes[2].set_ylim(-8,500)
    axes[1].legend(fontsize=9); axes[2].legend(fontsize=9)
    peak=results['default'][1]
    lags=np.arange(1,len(peak.acf))
    axes[3].plot(lags*15,peak.acf[1:],label='ACF окна при пике score (256/32)')
    axes[3].plot(lags*15,peak.reference_acf[1:],label='ACF начального фона')
    if case.tooth_indices:
        index=case.phases[2]['end']-1
        cfg=SETTINGS['default']
        teeth=ACFDetector(**cfg).fit(case.values[:cfg['reference_window']]).analyze(case.values[index-cfg['window']+1:index+1])
        axes[3].plot(lags*15,teeth.acf[1:],ls='--',color='#d97706',label='ACF первого высокого плато с пиками')
    axes[3].fill_between(lags*15,np.array(peak.reference_acf[1:])-peak.thresholds[1:],
                         np.array(peak.reference_acf[1:])+peak.thresholds[1:],color='#94a3b8',alpha=.18,label='Эвристическая полоса')
    axes[3].set_xlabel('Лаг, секунды (1 лаг = 15 с)'); axes[3].set_ylabel('ACF')
    axes[3].set_ylim(-1.1,1.1); axes[3].grid(alpha=.2); axes[3].legend(fontsize=8,ncol=2)
    zoom=case.zoom_index if case.zoom_index is not None else int(np.argmax(np.abs(np.diff(case.values))))+1
    left=max(0,zoom-8); right=min(len(times),zoom+9)
    axes[4].plot(times[left:right],case.values[left:right],drawstyle='default' if case.phases else 'steps-post',marker='o',ms=3,color=trace_color)
    axes[4].scatter(times[zoom],case.values[zoom],color='#dc2626',zorder=5)
    axes[4].xaxis.set_major_locator(mdates.SecondLocator(bysecond=(0,30),tz=MSK))
    axes[4].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S',tz=MSK))
    axes[4].set_title('Исходные точки крупно: каждый отсчёт через 15 секунд',fontsize=10)
    axes[4].set_xlabel('Europe/Moscow (UTC+03:00)'); axes[4].grid(alpha=.2)
    fig.savefig(output/('replay-'+case.name+'.png'),dpi=110); plt.close(fig)


def create_appendix(output):
    output.mkdir(parents=True,exist_ok=True)
    metadata=json.loads((FIXTURES/(SNAPSHOT+'.json')).read_text(encoding='utf-8'))
    cases=[]
    all_results={}
    for case in replay_scenarios():
        variants={}
        results={}
        for key,settings in SETTINGS.items():
            rows,peak,peak_index=replay_case(case,settings)
            results[key]=(rows,peak,peak_index)
            stem='replay-'+case.name+'-'+key
            save_csv(output/(stem+'.csv'),rows)
            save_csv(output/(stem+'-reference.csv'),[{k:r[k] for k in ('index','timestamp_utc','timestamp_msk','value')}
                     for r in rows[:settings['reference_window']]])
            save_csv(output/(stem+'-peak-lags.csv'),[{'lag':k,'lag_seconds':k*15,'acf':peak.acf[k],
                     'reference_acf':peak.reference_acf[k],'change':peak.lag_changes[k],
                     'threshold':peak.thresholds[k],'significant':k in peak.significant_lags}
                     for k in range(settings['max_lag']+1)])
            variants[key]={**summarize(case,rows,peak,peak_index,settings),'stem':stem}
            if case.tooth_indices:
                index=case.phases[2]['end']-1
                tooth=ACFDetector(**settings).fit(case.values[:settings['reference_window']]).analyze(
                    case.values[index-settings['window']+1:index+1])
                variants[key]['steady_teeth']={'index':index,'timestamp_msk':rows[index]['timestamp_msk'],
                                               'window_start_msk':rows[index-settings['window']+1]['timestamp_msk'],
                                               **asdict(tooth)}
                save_csv(output/(stem+'-teeth-lags.csv'),[{'lag':k,'lag_seconds':k*15,'acf':tooth.acf[k],
                         'reference_acf':tooth.reference_acf[k],'change':tooth.lag_changes[k],
                         'threshold':tooth.thresholds[k],'significant':k in tooth.significant_lags}
                         for k in range(settings['max_lag']+1)])
        render_case(output,case,results)
        info={'name':case.name,'label':case.label,'source':case.source,'points':len(case.values),
              'data_sha256':hashlib.sha256(np.asarray(case.values,dtype='<f8').tobytes()).hexdigest(),
              'image':'replay-'+case.name+'.png','variants':variants}
        cases.append(info)
        all_results[case.name]=(case,results)
    fig,axes=plt.subplots(3,1,figsize=(14,10),layout='constrained',sharex=True)
    for name,(case,results) in all_results.items():
        if name.startswith('influx-scenario-'):
            continue
        host=name.removeprefix('influx-')
        axes[0].plot(case.timestamps,case.values,lw=.6,drawstyle='steps-post',label=host)
        for i,key in enumerate(SETTINGS,1):
            rows=results[key][0]
            axes[i].plot(case.timestamps,[r['score'] if r['ready'] else np.nan for r in rows],lw=.7,label=host)
    axes[0].set_title('Те же четыре ряда Cassandra из InfluxDB: реакция ACFDetector')
    axes[0].set_ylabel('мс'); axes[1].set_ylabel('Score 256/32'); axes[2].set_ylabel('Score 40/10')
    axes[2].xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK,minticks=4,maxticks=8))
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter('%d.%m\n%H:%M',tz=MSK))
    axes[2].set_xlabel('Europe/Moscow (UTC+03:00)')
    for ax in axes:
        ax.grid(alpha=.2); ax.legend(fontsize=9,ncol=4)
    fig.savefig(output/'replay-influx-overview.png',dpi=110); plt.close(fig)
    for path in FIXTURES.glob(SNAPSHOT+'*'):
        shutil.copy2(path,output/path.name)
    image_metadata=load_snapshot()[1]
    reference=FIXTURES/(STEM+'-reference.png')
    if reference.is_file():
        shutil.copy2(reference,output/'replay-screenshot-reference.png')
    for path in FIXTURES.glob(STEM+'*'):
        shutil.copy2(path,output/path.name)
    result={'date':'2026-11-11','sample_interval_seconds':15,'series_count':len(cases),'evaluation_variants':len(cases)*2,
            'input_points':sum(c['points'] for c in cases),'settings':SETTINGS,'influx_source':metadata,
            'acf_class_sha256':hashlib.sha256(inspect.getsource(ACFDetector).encode()).hexdigest(),
            'generator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'fixture_loader_sha256':hashlib.sha256(Path(__file__).with_name('acf_replay_scenarios.py').read_bytes()).hexdigest(),
            'reference_image':'replay-screenshot-reference.png' if reference.is_file() else None,
            'scenario_source':image_metadata,'comparison_image':STEM+'-comparison.png','cases':cases}
    save_csv(output/'replay-summary.csv',[{'name':c['name'],'config':key,'points':c['points'],
             **{k:v[k] for k in ('ready_samples','nonzero_samples','max_score','depth_at_peak_score','max_depth','first_nonzero_msk')}}
             for c in cases for key,v in c['variants'].items()])
    (output/'replay-summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return result


def render_appendix(appendix):
    if not appendix:
        return ''
    def num(value):
        return '—' if value is None else f'{value:.5g}'
    table=''.join(f'<tr><td><a href="#replay-{c["name"]}">{html.escape(c["label"])}</a></td><td>{key}</td>'
                  f'<td>{num(v["max_score"])}</td><td>{v["depth_at_peak_score"]}/{v["max_depth"]}</td>'
                  f'<td>{v["nonzero_samples"]}/{v["ready_samples"]}</td><td>{v["first_nonzero_msk"] or "—"}</td></tr>'
                  for c in appendix['cases'] for key,v in c['variants'].items())
    content=f'''<section id="replay-inputs"><h2>Обновлено 11 ноября: Сценарий из InfluxDB, 17–18 октября</h2>
<p>Четыре цветных TPS-ряда «Сценария» из нового снимка InfluxDB и четыре прежних ряда Cassandra: {appendix['input_points']:,} исходных точек,
все через 15 секунд. ACF и CUSUM используют один и тот же сохранённый снимок. Две конфигурации каждого ряда:
default — window=256, max_lag=32, reference_window=512; short — 40/10/160.
Threshold=3.5 и min_change=0.1 для всех. Параметры не подбирались по реакциям отдельных host.</p>
<p>Первые M точек принимаются за фон из самого ряда. Следующие N заполняют отдельное локальное окно.
У default первый готовый результат на индексе 767 (191 мин 45 с от начала), у short — 199 (49 мин 45 с).
Серый участок — обучение фона default, светло-жёлтый — его локальный прогрев. Пропуски на графике score означают ready=False,
а нулевая линия после прогрева — измеренное отсутствие превышения. Фон фиксирован, будущие значения не используются.</p>
<p>Это оценка изменения ACF относительно выбранного начального участка. Начальный участок Cassandra не гарантированно стационарный
или здоровый. Положительный score означает превышение эвристической полосы по лагам; бинарный порог тревоги здесь не задан.
Количество ненулевых оценок относится к перекрывающимся окнам, а не независимым инцидентам.
Depth — последний значимый лаг, а не время начала смены или задержка обнаружения.</p>
<p>При нулевой энергии ACF статистически не определён; рабочий API возвращает нулевой профиль с zero_energy=True.
Если начальный фон был коррелированным, такое постоянное окно может получить ненулевой score как потерю прежних связей.
Ось score на графиках увеличена под наблюдаемый диапазон; граница API остаётся 1.</p>
<p>Максимум внутри размеченной фазы оценивает всё текущее окно и может сохранять реакцию на предыдущую фазу.
После скачка восстановленного уровня этот переход уходит из окна только после его полного заполнения.</p>
<table><tr><th>Ряд</th><th>Настройки</th><th>Max score</th><th>Depth на пике / max</th><th>Ненулевых / готовых</th><th>Первый score&gt;0 MSK</th></tr>{table}</table>
<p><a href="replay-summary.json">Параметры, ACF и сводка JSON</a> · <a href="replay-summary.csv">Сводка CSV</a></p></section>'''
    for case in appendix['cases']:
        if case['name'].startswith('influx-scenario-'):
            note=('3956 точек из InfluxDB: measurement screenshot_step_replay, поле value, цвет задаётся тегом trace. '
                  '17 октября 2026 14:23:30 — 18 октября 06:52:15, Москва; единицы TPS. '
                  'Снимок напрямую прочитан из БД и поточечно сверён с Grafana. '
                  'Исправленная реконструкция сохраняет ширину пиков, наклонные участки и откаты. '
                  'В оранжевом ряду семь откатов ниже первого высокого плато занимают 5–14 отсчётов. '
                  'Это реконструкция изображений, ранее загруженная в БД; исходной телеметрии нет. '
                  'Скрытые нижние перекрытия приближённые.')
            d=case['variants']['default']['steady_teeth']
            short=case['variants']['short']['steady_teeth']
            note+=(f' Отдельное окно на первом высоком плато без ступени входа: default score={d["score"]:.5f}, '
                   f'depth={d["depth"]}; short score={short["score"]:.5f}, depth={short["depth"]}. '
                   f'Значимые лаги default: {d["significant_lags"] or "нет"}; short: {short["significant_lags"] or "нет"}. '
                   'Все значения, включая промежуточные точки откатов, взяты из сохранённого снимка InfluxDB. '
                   'Профиль показан пунктиром отдельно от профиля на максимуме score.')
        else:
            note=('Те же 5761 значения за 1 октября 2026 12:30 — 2 октября 2026 12:30 MSK, обе границы включены. '
                  'Снимок работающей InfluxDB ранее поточечно проверен через запрос Grafana. '
                  'В compose это replay, реконструированный со скриншота, без эталонной разметки изменений ACF.')
        for key,v in case['variants'].items():
            if v['peak']['zero_energy']:
                note+=(f' Максимум {key} получен на постоянном окне (zero_energy=True): '
                       'его условный нулевой профиль отличается от коррелированного начального фона. '
                       'Этот score описывает исчезновение связей, а не рост значения метрики.')
        links=''.join(f'<p>{key}: <a href="{v["stem"]}.csv">все значения и решения</a> · '
                       f'<a href="{v["stem"]}-reference.csv">фон</a> · <a href="{v["stem"]}-peak-lags.csv">лаги на пике</a></p>'
                       for key,v in case['variants'].items())
        if case['name'].startswith('influx-scenario-'):
            links+=''.join(f'<p>{key}: <a href="{v["stem"]}-teeth-lags.csv">профиль окна только с зубьями</a></p>'
                           for key,v in case['variants'].items())
        phases=''.join(f'<tr><td>{html.escape(p["label"])}</td><td>{key}</td><td>{num(p["max_score"])}</td>'
                        f'<td>{p["nonzero_samples"]}/{p["ready_samples"]}</td><td>{num(p["max_depth"])}</td></tr>'
                        for key,v in case['variants'].items() for p in v['phases'])
        phase_table=(f'<table><tr><th>Фаза</th><th>Настройки</th><th>Max score</th><th>Ненулевых/готовых</th><th>Max depth</th></tr>{phases}</table>' if phases else '')
        comparison=(f'<img src="{appendix["comparison_image"]}" alt="Исходное изображение и восстановленная оранжевая линия">'
                    if case['name']=='influx-scenario-orange' else '')
        content+=f'''<section id="replay-{case['name']}"><h2>{html.escape(case['label'])}</h2><p>{note}</p>{comparison}
<img src="{case['image']}" alt="Исходный ряд, score, depth и ACF">{phase_table}{links}</section>'''
        if case['name']=='influx-scenario-orange' and appendix.get('reference_image'):
            content+=f'<details><summary>Исходный скриншот пользователя</summary><img src="{appendix["reference_image"]}" alt="Исходный скриншот"></details>'
        if case['name']=='influx-scenario-blue':
            content+='''<section id="replay-influx"><h2>Четыре ряда Cassandra: общий вид</h2>
<img src="replay-influx-overview.png" alt="Cassandra response time и ACF score">
<p><a href="trend-cassandra-15s.csv">Снимок всех исходных точек</a> · <a href="trend-cassandra-15s-query.flux">Flux</a> ·
<a href="trend-cassandra-15s.json">Проверка источника и SHA256</a></p></section>'''
    content+=f'<p><a href="{STEM}.csv">Все четыре TPS-ряда из InfluxDB</a> · <a href="{STEM}-query.flux">Flux</a> · <a href="{STEM}.json">Происхождение и SHA256</a></p>'
    return content
