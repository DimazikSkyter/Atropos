"""Offline charts from an existing run; never calls Docker or regenerates measurements.

Usage: python plot_results.py results/<run> [--output <directory>]
Requires matplotlib >= 3.8 and numpy. Writes PNG and SVG.
"""
import argparse
import datetime as dt
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import MaxNLocator

COLORS = {'api_get':'#2563eb', 'api_post':'#d97706', 'end_to_end_write':'#8b5cf6',
          'db_read':'#0891b2', 'db_write':'#c2410c', 'kafka_produce':'#c2410c',
          'consumer_callback':'#0891b2'}
NODES = {'api1:8000':'#1d4ed8','api2:8000':'#60a5fa',
         'consumer1:8000':'#047857','consumer2:8000':'#34d399'}


def build(run, output):
    manifest = json.loads((run/'manifest.json').read_text())
    summary = json.loads((run/'summary.json').read_text())
    if manifest.get('status') != 'completed':
        raise ValueError('Refusing to label an incomplete run as verified')
    phases = {e['phase']:e['timestamp'] for e in manifest['events']}
    origin = phases[1]
    run_date = dt.datetime.fromtimestamp(origin, dt.timezone.utc).strftime('%d.%m.%Y')
    fault_start, fault_end = phases[2]-origin, phases[3]-origin
    load_end = fault_end + manifest['recovery_s']
    end = manifest['end']-origin
    disk = manifest['action'] == 'test1'

    def rows(name):
        data = json.loads((run/(name+'.json')).read_text())
        if data['status'] != 'success':
            raise ValueError(f'Invalid export: {name}')
        return data['data']['result']

    def plot(ax, name, predicate, label, color, factor=1, logarithmic=False):
        matching = [r for r in rows(name) if predicate(r['metric'])]
        if len(matching) != 1:
            raise ValueError(f'Expected one series for {name} / {label}, found {len(matching)}')
        values = np.array(matching[0]['values'], dtype=float)
        x, y = values[:,0]-origin, values[:,1]*factor
        y[~np.isfinite(y)] = np.nan
        if logarithmic:
            y[y <= 0] = np.nan
        ax.plot(x,y,color=color,label=label,lw=1.9)

    def by_stage(ax,name,stages):
        for stage,label in stages:
            plot(ax,name,lambda m,s=stage: m.get('stage')==s and m.get('result')=='ok',
                 label,COLORS[stage],1000,True)
        ax.set_yscale('log')
        ax.set_ylabel('мс · лог. шкала')

    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.titlesize':12,
                         'axes.labelsize':10,'legend.fontsize':9,'svg.fonttype':'none',
                         'axes.spines.top':False,'axes.spines.right':False,
                         'axes.edgecolor':'#cbd5e1','text.color':'#172033',
                         'axes.labelcolor':'#334155','xtick.color':'#475569','ytick.color':'#475569'})
    fig, axes = plt.subplots(3,2,figsize=(15,11.5),sharex=True)
    fig.set_facecolor('#ffffff')
    ax = axes.ravel()
    by_stage(ax[0],'mean',[('api_get','GET: чтение'),('api_post','POST: Kafka ACK'),
                           ('end_to_end_write','Запись до commit БД')])
    ax[0].set_title('1. Бизнесовый отклик: скользящее среднее',loc='left')
    if disk:
        by_stage(ax[1],'mean',[('kafka_produce','Ожидание Kafka'),('consumer_callback','Callback consumer')])
    else:
        by_stage(ax[1],'mean',[('db_read','DB read + пул'),('db_write','DB write + commit + пул')])
    ax[1].set_title('2. Внутренние этапы: скользящее среднее',loc='left')
    for node,color in NODES.items():
        plot(ax[2],'cpu',lambda m,n=node: m.get('instance')==n,
             node.split(':')[0],color,100)
    ax[2].set_title('3. CPU: одно ядро на контейнер',loc='left')
    ax[2].set_ylabel('% одного ядра')
    ax[2].set_ylim(0,110)
    ax[2].axhline(80,color='#64748b',ls=':',lw=1)

    # Sum only timestamps available in every partition; mask unavailable independent probes.
    lag = [{float(t):float(v) for t,v in r['values']} for r in rows('lag')]
    probe_rows = rows('probe_ok')
    probe = {float(t):float(v) for t,v in probe_rows[0]['values']}
    times = sorted(set.intersection(*(set(r) for r in lag)))
    total = [sum(r[t] for r in lag) if probe.get(t)==1 else np.nan for t in times]
    ax[3].plot(np.array(times)-origin,total,color='#7c3aed',lw=2,label='Сумма по 6 партициям')
    ax[3].set_title('4. Очередь Kafka: непройденные offsets',loc='left')
    ax[3].set_ylabel('сообщений')
    ax[3].set_ylim(bottom=0)

    by_stage(ax[4],'stddev',[('api_get','GET'),('api_post','POST'),('end_to_end_write','Запись до commit БД')])
    ax[4].set_title('5. Разброс времени: стандартное отклонение σ',loc='left')
    if disk:
        values = np.array(rows('disk_blocked')[0]['values'],dtype=float)
        ax[5].step(values[:,0]-origin,values[:,1],where='post',color='#dc2626',lw=2,
                   label='Фактически заблокированные syscall')
        ax[5].set_title('6. Доказательство: файловые вызовы kafka1',loc='left')
        ax[5].set_ylabel('одновременных вызовов')
        ax[5].yaxis.set_major_locator(MaxNLocator(integer=True))
    else:
        for node in ('api1:8000','api2:8000'):
            plot(ax[5],'inflight',lambda m,n=node: m.get('instance')==n,
                 node.split(':')[0],NODES[node])
        ax[5].set_title('6. Активные HTTP-запросы в API',loc='left')
        ax[5].set_ylabel('запросов на ноду')
        ax[5].axhline(256,color='#64748b',ls=':',lw=1)
    ax[5].set_ylim(bottom=0)

    for a in ax:
        a.axvspan(fault_start,fault_end,color='#ef4444',alpha=.10,zorder=-1)
        a.axvline(fault_start,color='#ef4444',alpha=.6,lw=.8)
        a.axvline(fault_end,color='#ef4444',alpha=.6,lw=.8)
        a.axvline(load_end,color='#64748b',ls='--',lw=1)
        a.set_xlim(0,end)
        a.xaxis.set_major_locator(MaxNLocator(7))
        a.grid(True,which='major',alpha=.20,color='#94a3b8')
        a.legend(loc='upper left',framealpha=.9,edgecolor='none',ncol=1)

    title = 'Kafka: зависание дискового I/O одной ноды' if disk else 'PostgreSQL: задержка сети обоих consumer'
    metrics = summary['metrics']
    requests = int(metrics['http_reqs']['values']['count'])
    errors = int(metrics['http_req_failed']['values']['passes'])
    dropped = int(metrics['dropped_iterations']['values']['count'])
    fig.suptitle(title,x=.065,y=.974,ha='left',fontsize=20,weight='bold')
    fig.text(.065,.942,f"{manifest['rate']} HTTP RPS · 50% POST / 50% GET · воздействие {fault_start:.0f}–{fault_end:.0f} с · {run_date} UTC",fontsize=12)
    fig.text(.065,.919,f'{requests:,} запросов · {errors:,} HTTP-ошибок за весь прогон · {dropped} пропущенных итераций k6'.replace(',',' '),fontsize=11,color='#475569')
    fig.subplots_adjust(left=.065,right=.983,top=.865,bottom=.125,hspace=.39,wspace=.24)
    fig.supxlabel('Время от начала нагрузки, с',y=.080,fontsize=12)
    fig.text(.065,.044,'Реальные измерения. CPU, среднее и σ — окна 20 с; latency — только успешные завершённые операции.',fontsize=10,color='#475569')
    fig.text(.065,.025,'Красная область — воздействие. Пунктир — ≈ остановка нагрузки. Разрывы — нет валидных данных; значения не дорисованы.',fontsize=10,color='#475569')
    output.mkdir(parents=True,exist_ok=True)
    stem = 'kafka-disk-stall' if disk else 'postgres-network-lag'
    for extension in ('png','svg'):
        path = output/(stem+'.'+extension)
        fig.savefig(path,dpi=150,facecolor=fig.get_facecolor())
        print(path.resolve())
    plt.close(fig)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    build(args.run,args.output or args.run/'plots')
