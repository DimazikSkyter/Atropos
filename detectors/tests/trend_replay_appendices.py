"""Append screenshot-shaped and actual local InfluxDB replay observations.

The fixture refresh issues query/read requests only; it never seeds or changes
InfluxDB, Grafana, dashboards, or Docker services. Credentials are not exported.
"""

import argparse
import base64
import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import html
import http.client
import io
import json
import math
import os
from pathlib import Path
import shutil
import urllib.parse

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from apps.detectors import TrendDetector


MSK=timezone(timedelta(hours=3))
START='2026-10-01T09:30:00Z'
STOP='2026-10-02T09:30:00Z'
UID='cassandra-response-time-replay'
FIXTURES=Path(__file__).with_name('fixtures')
SNAPSHOT='trend-cassandra-15s'
SCREENSHOT='screenshot-step-replay'
CONFIG={'sample_interval':15,'time_scale':60,'value_scale':1}


def save_csv(path, rows):
    with path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def parse_flux_csv(text):
    """Annotated Flux CSV can repeat headers and change column order per table."""
    header=None
    result=[]
    for fields in csv.reader(io.StringIO(text)):
        if not fields or fields[0].startswith('#'):
            continue
        if '_time' in fields and '_value' in fields:
            header=fields
            continue
        if header is None:
            raise ValueError('Flux data before header')
        row=dict(zip(header,fields))
        if row.get('_time'):
            value=float(row['_value'])
            if not math.isfinite(value):
                raise ValueError('non-finite InfluxDB value')
            timestamp=datetime.fromisoformat(row['_time'].replace('Z','+00:00'))
            result.append({'timestamp_utc':timestamp.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
                           'host':row['host'],'value_ms':value})
    return sorted(result,key=lambda r:(r['host'],r['timestamp_utc']))


def fetch_cassandra_snapshot(fixtures=FIXTURES, infra_root=None):
    """Read the live dashboard, InfluxDB query, and matching Grafana frames."""
    settings={}
    if infra_root is not None and (Path(infra_root)/'.env').is_file():
        for line in (Path(infra_root)/'.env').read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key,value=line.split('=',1)
                settings[key.strip()]=value.strip().strip('\"\'')
    def setting(name, default):
        return os.getenv(name,settings.get(name,default))
    grafana=setting('GRAFANA_URL','http://localhost:'+setting('GRAFANA_PORT','3001')).rstrip('/')
    influx=setting('INFLUX_URL','http://localhost:'+setting('INFLUXDB_PORT','8086')).rstrip('/')
    org=setting('INFLUXDB_ORG','atropos')
    bucket=setting('INFLUXDB_BUCKET','metrics-replay')
    token=setting('INFLUXDB_TOKEN','atropos-local-replay-token')
    authorization='Basic '+base64.b64encode((setting('GRAFANA_ADMIN_USER','admin')+':'+setting('GRAFANA_ADMIN_PASSWORD','admin')).encode()).decode()
    def request(url, data=None, headers=None):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='http' or parsed.hostname not in ('localhost','127.0.0.1','::1'):
            raise ValueError('This fixture reader accepts local HTTP endpoints only')
        connection=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=30)
        try:
            connection.request('POST' if data is not None else 'GET',parsed.path+('?' + parsed.query if parsed.query else ''),
                               body=data,headers=headers or {})
            response=connection.getresponse()
            text=response.read().decode('utf-8')
            if response.status!=200:
                raise RuntimeError(f'Local read request failed: HTTP {response.status} at {parsed.path}')
            return text
        finally:
            connection.close()
    live=json.loads(request(grafana+'/api/dashboards/uid/'+UID,headers={'Authorization':authorization}))
    dashboard=live['dashboard']
    panels=[p for p in dashboard['panels'] if p.get('targets')]
    assert len(panels)==1,'Inspect changed dashboard before importing another set of graphs'
    panel=panels[0]
    template=panel['targets'][0]['query']
    assert 'cassandra_response_time' in template and 'aggregateWindow' not in template
    flux=(template.replace('v.defaultBucket',json.dumps(bucket))
          .replace('v.timeRangeStart',START).replace('v.timeRangeStop',STOP)
          .replace('${host:regex}','.*'))
    raw=request(influx+'/api/v2/query?'+urllib.parse.urlencode({'org':org}),flux.encode(),
                {'Authorization':'Token '+token,'Content-Type':'application/vnd.flux','Accept':'application/csv'})
    points=parse_flux_csv(raw)
    groups=group_points(points)
    assert set(groups)=={f'cassandra-{i}' for i in range(1,5)}
    for rows in groups.values():
        stamps=[datetime.fromisoformat(r['timestamp_utc'].replace('Z','+00:00')) for r in rows]
        assert len(rows)==5761 and stamps[0].isoformat().startswith('2026-10-01T09:30:00')
        assert stamps[-1]-stamps[0]==timedelta(days=1)
        assert all(b-a==timedelta(seconds=15) for a,b in zip(stamps,stamps[1:]))
    # Cross-check the same source through the dashboard's datasource query API.
    payload={'from':str(int(datetime.fromisoformat(START.replace('Z','+00:00')).timestamp()*1000)),
             'to':str(int(datetime.fromisoformat(STOP.replace('Z','+00:00')).timestamp()*1000)),
             'queries':[{'refId':'A','datasource':panel['datasource'],'query':flux,
                         'maxDataPoints':10000,'intervalMs':15000}]}
    response=json.loads(request(grafana+'/api/ds/query',json.dumps(payload).encode(),
                               {'Authorization':authorization,'Content-Type':'application/json'}))
    frames=response['results']['A']['frames']
    observed={}
    for frame in frames:
        fields=frame['schema']['fields']
        values=frame['data']['values']
        time_index=next(i for i,f in enumerate(fields) if f['type']=='time')
        for i,field in enumerate(fields):
            if field['type']!='number':
                continue
            host=field.get('labels',{}).get('host')
            if host:
                observed[host]=[(int(t),float(v)) for t,v in zip(values[time_index],values[i])]
    expected={host:[(int(datetime.fromisoformat(r['timestamp_utc'].replace('Z','+00:00')).timestamp()*1000),r['value_ms'])
                    for r in rows] for host,rows in groups.items()}
    assert observed==expected,'Grafana frames differ from direct InfluxDB results'
    fixtures.mkdir(parents=True,exist_ok=True)
    path=fixtures/(SNAPSHOT+'.csv')
    save_csv(path,points)
    (fixtures/(SNAPSHOT+'-query.flux')).write_text(flux,encoding='utf-8')
    (fixtures/(SNAPSHOT+'-raw.csv')).write_text(raw,encoding='utf-8')
    (fixtures/(SNAPSHOT+'-dashboard.json')).write_text(json.dumps(dashboard,ensure_ascii=False,indent=2),encoding='utf-8')
    metadata={'dashboard_uid':UID,'dashboard_title':dashboard['title'],'description':dashboard.get('description',''),
              'grafana_url':grafana+'/d/'+UID+'?'+urllib.parse.urlencode({'orgId':1,'from':START,'to':STOP,'timezone':'Europe/Moscow','var-host':'$__all'}),
              'influx_url':influx,'org':org,'bucket':bucket,'measurement':'cassandra_response_time','field':'value',
              'start_utc':START,'stop_utc_inclusive':STOP,'timezone':'Europe/Moscow','interval_seconds':15,
              'counts':{h:len(r) for h,r in groups.items()},'total_points':len(points),
              'csv_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'grafana_frames_equal_influx':True,
              'source_kind':'Live InfluxDB snapshot of screenshot-reconstructed replay; not raw production telemetry',
              'config':CONFIG,'read_only':True}
    (fixtures/(SNAPSHOT+'.json')).write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    return metadata


def group_points(points):
    groups={}
    for row in points:
        groups.setdefault(row['host'],[]).append(row)
    return {host:sorted(rows,key=lambda r:r['timestamp_utc']) for host,rows in sorted(groups.items())}


def screenshot_metadata():
    return json.loads((FIXTURES/(SCREENSHOT+'.json')).read_text(encoding='utf-8'))


def screenshot_fixture():
    """Actual digitization of the reattached orange trace, two step episodes."""
    metadata=screenshot_metadata()
    path=FIXTURES/(SCREENSHOT+'.csv')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=metadata['csv_sha256']:
        raise ValueError('Screenshot fixture changed: rerun calibrated digitization')
    with path.open(encoding='utf-8',newline='') as stream:
        rows=list(csv.DictReader(stream))
    values=np.array([float(r['value']) for r in rows])
    background=np.array([float(r['background']) for r in rows])
    timestamps=[datetime.fromisoformat(r['timestamp_msk']) for r in rows]
    peaks=np.array([int(r['index']) for r in rows if r['is_peak']=='True'],dtype=int)
    return values,timestamps,metadata['phases'],peaks,background


def replay(values,timestamps):
    detector=TrendDetector(**CONFIG)
    rows=[]
    for i,(value,stamp) in enumerate(zip(values,timestamps)):
        r=detector.update(float(value))
        rows.append({'index':i,'timestamp_utc':stamp.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
                     'timestamp_msk':stamp.astimezone(MSK).isoformat(),'elapsed_seconds':i*15,
                     'value':float(value),**asdict(r)})
    return rows


def describe(rows):
    events={r['confirmed_at']:r for r in rows if r['detected']}
    return {'points':len(rows),'ready_samples':sum(r['ready'] for r in rows),
            'max_score':max(r['score'] for r in rows),'alarm_samples':sum(r['detected'] for r in rows),
            'confirmed_events':len(events),
            'events':[{'estimated_start':r['change_start'],'confirmed_at':k,'direction':r['direction'],
                       'start_msk':rows[r['change_start']]['timestamp_msk'],
                       'confirmed_msk':rows[k]['timestamp_msk']} for k,r in sorted(events.items())]}


def plot_series(output,stem,title,values,timestamps,rows,phases=(),zoom_index=None):
    t=mdates.date2num(timestamps)
    fig,axes=plt.subplots(4,1,figsize=(14,11),layout='constrained',gridspec_kw={'height_ratios':[3,1.4,1.4,2]})
    fig.suptitle(title+'\nШаг 15 с; окна 40/240 точек (≈10/60 мин); память 13 точек',fontsize=12)
    axes[0].set_facecolor('#181d24')
    axes[0].plot(t,values,color='#f59e0b',lw=.8,marker='.',ms=1.2,drawstyle='steps-post',label='Исходные точки')
    events=describe(rows)['events']
    for i,event in enumerate(events):
        k=event['confirmed_at']; start=event['estimated_start']
        axes[0].scatter([t[k]],[values[k]],marker='^',color='#dc2626',s=45,zorder=5,label='Подтверждение' if i==0 else None)
        axes[0].scatter([t[start]],[values[start]],marker='o',facecolors='none',edgecolors='#06b6d4',s=40,zorder=5,
                        label='Оценка начала' if i==0 else None)
    for phase in phases[1:]:
        axes[0].axvline(t[phase['start']],color='#94a3b8',ls=':',lw=.7)
    axes[0].set_ylabel('Относительные ед.' if phases else 'мс')
    axes[1].plot(t,[r['score'] for r in rows],color='#dc2626',label='Накопленный score')
    axes[1].plot(t,[r['evidence'] for r in rows],color='#94a3b8',lw=.6,label='Evidence одной точки')
    axes[1].axhline(.8,color='#991b1b',ls='--',lw=.8,label='Порог подтверждения')
    axes[1].set_ylim(-.03,1.04); axes[1].set_ylabel('0…1')
    angles=[r['angle'] if r['ready'] else np.nan for r in rows]
    axes[2].plot(t,angles,color='#2563eb',lw=.7,label='Угол концевого тренда')
    axes[2].axhspan(-1,1,color='#16a34a',alpha=.2,label='horizontal=True: ±1°')
    axes[2].set_ylabel('Градусы\nось значений / мин')
    for ax in axes[:3]:
        first_ready=next((i for i,r in enumerate(rows) if r['ready']),len(rows)-1)
        ax.axvspan(t[0],t[first_ready],color='#94a3b8',alpha=.13)
        ax.set_xlim(t[0],t[-1])
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK,minticks=4,maxticks=8))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m\n%H:%M',tz=MSK))
        ax.grid(alpha=.2); ax.legend(fontsize=8,ncol=3,loc='upper left')
    if zoom_index is None:
        zoom_index=int(np.argmax(np.abs(np.diff(values))))+1
    left=max(0,zoom_index-8); right=min(len(rows),zoom_index+9)
    axes[3].plot(t[left:right],values[left:right],color='#d97706',lw=1,marker='o',ms=4,drawstyle='steps-post')
    axes[3].scatter([t[zoom_index]],[values[zoom_index]],color='#dc2626',zorder=5,s=35)
    axes[3].set_title('Увеличение: каждая точка — отдельный отсчёт, шаг 15 секунд',fontsize=10)
    axes[3].xaxis.set_major_locator(mdates.SecondLocator(bysecond=(0,30),tz=MSK))
    axes[3].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S',tz=MSK))
    axes[3].set_xlabel('Europe/Moscow (UTC+03:00)'); axes[3].grid(alpha=.2)
    fig.savefig(output/(stem+'.png'),dpi=115)
    plt.close(fig)


def create_appendices(output,reference_image=None,fixtures=FIXTURES):
    output.mkdir(parents=True,exist_ok=True)
    values,timestamps,phases,teeth,_=screenshot_fixture()
    metadata=screenshot_metadata()
    rows=replay(values,timestamps)
    stem='screenshot-15s'
    save_csv(output/(stem+'-points.csv'),rows)
    highlighted=[p for p in metadata['peaks'] if phases[2]['start']<=p['index']<phases[2]['end']]
    zoom=max(highlighted,key=lambda p:abs(p['value']-p['background']))['index']
    plot_series(output,stem,'По скриншоту: два ступенчатых эпизода, зубья на первом высоком плато',
                values,timestamps,rows,phases,int(zoom))
    synthetic={**describe(rows),'stem':stem,'config':CONFIG,
               'kind':'Pixel digitization of the highlighted orange trace; normalized units; one sample per peak',
               'interval_seconds':15,'tooth_indices':teeth.tolist(),'phases':[],
               'data_sha256':metadata['value_sha256'],'digitization':metadata,
               'comparison_image':SCREENSHOT+'-comparison.png'}
    for phase in phases:
        part=rows[phase['start']:phase['end']]
        synthetic['phases'].append({**phase,'start_msk':part[0]['timestamp_msk'],'end_msk':part[-1]['timestamp_msk'],
                                    'max_score':max(r['score'] for r in part),
                                    'alarm_samples':sum(r['detected'] for r in part),
                                    'confirmations':sum(phase['start']<=e['confirmed_at']<phase['end'] for e in synthetic['events'])})
    reference_image=reference_image or FIXTURES/metadata['source_image']
    if hashlib.sha256(reference_image.read_bytes()).hexdigest()!=metadata['source_image_sha256']:
        raise ValueError('Reference image does not match the digitized screenshot')
    if reference_image.resolve()!=(output/'screenshot-reference.png').resolve():
        shutil.copy2(reference_image,output/'screenshot-reference.png')
    for path in FIXTURES.glob(SCREENSHOT+'*'):
        shutil.copy2(path,output/path.name)
    synthetic['reference_image']='screenshot-reference.png' if (output/'screenshot-reference.png').is_file() else None
    result={'screenshot':synthetic}
    snapshot=fixtures/(SNAPSHOT+'.csv')
    if snapshot.is_file():
        metadata=json.loads((fixtures/(SNAPSHOT+'.json')).read_text(encoding='utf-8'))
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest()==metadata['csv_sha256']
        with snapshot.open(encoding='utf-8',newline='') as stream:
            groups=group_points([{'timestamp_utc':r['timestamp_utc'],'host':r['host'],'value_ms':float(r['value_ms'])}
                                 for r in csv.DictReader(stream)])
        hosts=[]
        fig,axes=plt.subplots(2,1,figsize=(14,8),layout='constrained',sharex=True)
        for host,points in groups.items():
            stamps=[datetime.fromisoformat(r['timestamp_utc'].replace('Z','+00:00')) for r in points]
            vals=np.array([r['value_ms'] for r in points])
            rows=replay(vals,stamps)
            stem='influx-'+host
            save_csv(output/(stem+'-points.csv'),rows)
            plot_series(output,stem,'InfluxDB replay: '+host+' — Cassandra response time',vals,stamps,rows)
            info={**describe(rows),'host':host,'stem':stem,'config':CONFIG,
                  'data_sha256':hashlib.sha256(np.asarray(vals,dtype='<f8').tobytes()).hexdigest()}
            hosts.append(info)
            axes[0].plot(stamps,vals,lw=.6,label=host,drawstyle='steps-post')
            axes[1].plot(stamps,[r['score'] for r in rows],lw=.8,label=host)
        axes[0].set_title('Из InfluxDB: те же четыре ряда, что на дашборде Grafana; без агрегации')
        axes[0].set_ylabel('Response time, мс')
        axes[1].set_ylabel('Score 0…1'); axes[1].set_ylim(-.03,1.04)
        axes[1].axhline(.8,color='#dc2626',ls='--',lw=.8)
        axes[1].xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK,minticks=4,maxticks=8))
        axes[1].xaxis.set_major_formatter(mdates.DateFormatter('%d.%m\n%H:%M',tz=MSK))
        axes[1].set_xlabel('Europe/Moscow (UTC+03:00)')
        for ax in axes:
            ax.grid(alpha=.2); ax.legend(fontsize=9,ncol=4)
        fig.savefig(output/'influx-cassandra-overview.png',dpi=115); plt.close(fig)
        for path in fixtures.glob(SNAPSHOT+'*'):
            if path.is_file():
                shutil.copy2(path,output/path.name)
        result['influx']={'metadata':metadata,'hosts':hosts}
    (output/'appendices.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def appendices_html(appendices):
    if not appendices:
        return ''
    s=appendices['screenshot']
    phase_rows=''.join(f'<tr><td>{html.escape(p["label"])}</td><td>{p["start_msk"][11:19]}—{p["end_msk"][11:19]}</td>'
                       f'<td>{p["max_score"]:.3f}</td><td>{p["confirmations"]}</td><td>{p["alarm_samples"]}</td></tr>' for p in s['phases'])
    synthetic_events=''.join(f'<tr><td>{e["start_msk"]}</td><td>{e["confirmed_msk"]}</td><td>{e["direction"]:+d}</td></tr>' for e in s['events'])
    reference=(f'<details><summary>Исходный скриншот пользователя</summary><img src="{s["reference_image"]}" alt="Исходный скриншот"></details>'
               if s['reference_image'] else '')
    content=f'''<section id="screenshot-15s"><h2>Исправлено 4 ноября: оранжевая линия присланного графика</h2>
<p>Два ступенчатых эпизода: ноль → нижнее плато → высокое плато → ноль, затем повторение.
Первое высокое плато содержит пики вверх и вниз; второе спокойное. Значения извлечены из пикселей повторно присланного изображения,
с привязкой к отметкам 16:00…06:00. Отдельного пологого снижения и трёх восстанавливающих ступеней в этом наборе нет.</p>
<p>Оцифрована верхняя оранжевая линия, выделенная синей рамкой. Y-шкалы на изображении нет: ноль принят за 0,
нижнее плато за 100, высокое получается около 167. Даты 1–2 октября условные; часы взяты со скриншота.
Время переходов оценено по пикселям и округлено до сетки 15 секунд. Каждый из {len(s['tooth_indices'])} выделенных узких пиков
занимает одну точку согласно указанию пользователя. Межпиксельный фон интерполирован и может вносить короткую корреляцию.</p>
<img src="{s['comparison_image']}" alt="Исходный график и оцифрованная оранжевая линия">
<p>Конфигурация детектора 40/240, память 13; sample_interval=15, time_scale=60, value_scale=1.
Угол относится к изменению относительных единиц за минуту. Ни ряд, ни результаты не сглаживаются и не прореживаются.
Красный треугольник — фактическое подтверждение; голубой круг — оценённое начало, доступное после подтверждения.</p>
<p>Серый участок — прогрев первых 240 наблюдений: score=0 до готовности не является измеренным отсутствием смены.</p>
<p><b>Результат:</b> подтверждений {s['confirmed_events']}; точек detected=True {s['alarm_samples']}; max score={s['max_score']:.3f}.
Отсутствие подтверждения не означает отсутствия краткого смещения или отдельного выброса.</p>
{reference}<img src="{s['stem']}.png" alt="Реконструкция формы и реакция детектора">
<table><tr><th>Участок</th><th>Время MSK</th><th>Max score</th><th>Новых подтверждений</th><th>Точек detected</th></tr>{phase_rows}</table>
<table><tr><th>Оценка начала (условное время MSK)</th><th>Подтверждено</th><th>Изменение наклона</th></tr>{synthetic_events}</table>
<p><a href="{s['stem']}-points.csv">Все точки и решения CSV</a> · <a href="appendices.json">Параметры и события JSON</a></p></section>'''
    if 'influx' not in appendices:
        return content
    data=appendices['influx']; meta=data['metadata']
    overview=''.join(f'<tr><td><a href="#{h["stem"]}">{h["host"]}</a></td><td>{h["points"]}</td><td>{h["confirmed_events"]}</td>'
                     f'<td>{h["alarm_samples"]}</td><td>{h["max_score"]:.3f}</td></tr>' for h in data['hosts'])
    content+=f'''<section id="influx-cassandra"><h2>Дополнение: Cassandra response time из InfluxDB</h2>
<p><a href="{html.escape(meta['grafana_url'],quote=True)}">Указанный дашборд Grafana</a>.
Период: 1 октября 2026 12:30 — 2 октября 2026 12:30 MSK, обе границы включены.
Четыре host, по 5761 точке, шаг 15 секунд, всего 23 044 значения в миллисекундах.
Извлечено из работающей InfluxDB тем же Flux-запросом, с host=All; без aggregateWindow, усреднения, интерполяции и прореживания.
Ответ datasource Grafana поточечно сравнен с прямым ответом InfluxDB: совпадение полное.</p>
<p>Источник в compose — replay, ранее реконструированный со скриншота; это не сырая производственная телеметрия.
Для него нет достоверного эталона смены тренда: показаны фактические реакции и их отсутствие, без выдуманных recall и ошибок начала.
Параметры для всех host одинаковы: 40/240, память 13, sample_interval=15, time_scale=60, value_scale=1; slope в мс/мин.</p>
<img src="influx-cassandra-overview.png" alt="Четыре ряда из InfluxDB и их score">
<table><tr><th>Host</th><th>Точек</th><th>Подтверждений</th><th>Точек detected</th><th>Max score</th></tr>{overview}</table>
<p><a href="{SNAPSHOT}.csv">Снимок исходных точек</a> · <a href="{SNAPSHOT}-raw.csv">Неизменённый ответ InfluxDB</a> ·
<a href="{SNAPSHOT}-query.flux">Flux-запрос</a> · <a href="{SNAPSHOT}-dashboard.json">Живой дашборд JSON</a> ·
<a href="{SNAPSHOT}.json">Проверки источника и SHA256</a></p></section>'''
    for host in data['hosts']:
        events=''.join(f'<tr><td>{e["start_msk"]}</td><td>{e["confirmed_msk"]}</td><td>{e["direction"]:+d}</td></tr>' for e in host['events'])
        absent=('<p><b>Устойчивой смены не подтверждено.</b> Видимый рост или скачки ряда при этих настройках '
                'могут остаться без подтверждения; max score ниже 0.8. Это фактический результат данного прогона.</p>'
                if not host['confirmed_events'] else '')
        content+=f'''<section id="{host['stem']}"><h2>{host['host']}: ряд и реакция детектора</h2>
<p>Подтверждений {host['confirmed_events']}; max score={host['max_score']:.3f}; точек detected=True {host['alarm_samples']}.
Голубой круг — оценка начала, красный треугольник — подтверждение; нижний график показывает отдельные исходные точки через 15 секунд.
Угол горизонтальности ±1° задан в осях мс/мин. Уровневый скачок может менять локальный угол, не проходя подтверждение смены тренда.</p>
{absent}
<img src="{host['stem']}.png" alt="{host['host']}, исходный ряд и решения">
<table><tr><th>Оценка начала MSK</th><th>Подтверждено MSK</th><th>Изменение наклона</th></tr>{events}</table>
<p><a href="{host['stem']}-points.csv">Все исходные точки и решения CSV</a></p></section>'''
    return content


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch-influx',action='store_true')
    parser.add_argument('--infra-root',type=Path)
    args=parser.parse_args()
    if args.fetch_influx:
        metadata=fetch_cassandra_snapshot(infra_root=args.infra_root)
        print(json.dumps({k:metadata[k] for k in ('counts','total_points','grafana_frames_equal_influx','read_only')},indent=2))
