"""Offline PNG/SVG and phase statistics from saved real measurements (no Docker needed)."""
import argparse
import json
import math
from pathlib import Path
import statistics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

def build(run):
    manifest=json.loads((run/'manifest.json').read_text(encoding='utf-8'))
    if manifest['status']!='completed': raise ValueError('Run must be completed')
    def rows(key):
        data=json.loads((run/(key+'.json')).read_text(encoding='utf-8'))
        if data['status']!='success' or not data['data']['result']: raise ValueError('Missing export: '+key)
        return data['data']['result']
    phases={}
    for row in rows('phase_start'):
        valid=[float(v) for t,v in row['values'] if float(v)>=manifest['start']]
        if valid: phases[int(row['metric']['phase'])]=min(valid)
    if set(phases)!={0,1,2,3}: raise ValueError('Missing phase markers')
    origin=phases[0]; end=phases[3]+manifest['recovery']
    colors={'0':'#2563eb','1':'#ea580c'}
    labels={'0':'P0: один крупный клиент','1':'P1: 128 мелких клиентов'}
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.titlesize':12,
                         'axes.spines.top':False,'axes.spines.right':False,'svg.fonttype':'none'})
    fig,axes=plt.subplots(3,2,figsize=(15,11),sharex=True)
    panels=[('mean','Среднее время обработки сообщения','мс · лог. шкала',1000,True),
            ('hit_ratio','Cache hit ratio','%',100,False),
            ('stddev','Разброс времени обработки: σ','мс · лог. шкала',1000,True),
            ('produced','Входящий поток: распределение 80 / 20','сообщений / с',1,False),
            ('end_to_end','От API до результата consumer','мс · лог. шкала',1000,True),
            ('lag','Kafka lag: ещё не закоммиченные offsets','сообщений',1,False)]
    probe={float(t):float(v) for t,v in rows('probe')[0]['values']}
    for ax,(key,title,unit,factor,log) in zip(axes.ravel(),panels):
        for row in rows(key):
            part=row['metric'].get('partition')
            if part not in colors: continue
            values=np.array(row['values'],dtype=float); x=values[:,0]-origin; y=values[:,1]*factor
            y[~np.isfinite(y)]=np.nan
            if log: y[y<=0]=np.nan
            if key=='lag':
                for i,t in enumerate(values[:,0]):
                    if probe.get(t)!=1: y[i]=np.nan
            ax.plot(x,y,color=colors[part],label=labels[part],lw=1.7)
        ax.axvspan(0,phases[1]-origin,color='#94a3b8',alpha=.14)
        ax.axvspan(phases[2]-origin,phases[3]-origin,color='#ef4444',alpha=.09)
        for phase in (1,2,3): ax.axvline(phases[phase]-origin,color='#64748b',ls='--',lw=.8)
        ax.set(title=title,ylabel=unit,xlim=(0,end-origin))
        if log: ax.set_yscale('log')
        else: ax.set_ylim(bottom=0)
        if key=='hit_ratio': ax.set_ylim(-2,105)
        ax.grid(alpha=.22); ax.legend(loc='best',fontsize=9)
    summary=json.loads((run/'summary.json').read_text())['metrics']
    count=int(summary['http_reqs']['values']['count'])
    fig.suptitle('Разнообразие ключей → промахи кэша → замедление consumer',x=.065,y=.976,ha='left',fontsize=19,weight='bold')
    fig.text(.065,.937,f"{manifest['rate']} HTTP RPS · кэш 256 записей на consumer · {count:,} запросов · прогон {manifest['id']}".replace(',',' '),fontsize=11)
    fig.subplots_adjust(left=.065,right=.985,top=.875,bottom=.13,hspace=.35,wspace=.23)
    fig.supxlabel('Время от первого запроса, с',y=.077)
    fig.text(.065,.04,'Серое — прогрев. Красное — разнообразные ключи у мелких клиентов. Затем возврат к повторяемым ключам.',fontsize=10)
    fig.text(.065,.021,'Реальные Micrometer → VictoriaMetrics → JSON; окна 20 с, шаг 2 с. Среднее/σ относятся к обработке, без ожидания в Kafka.',fontsize=10)
    output=run/'plots'; output.mkdir(exist_ok=True)
    for ext in ('png','svg'): fig.savefig(output/('cache-sharding.'+ext),dpi=150)
    plt.close(fig)
    stats={}
    for phase,finish in ((1,phases[2]),(2,phases[3]),(3,end)):
        stats[str(phase)]={}
        for key in ('mean','stddev','p95','p99','hit_ratio','produced','consumed','db','end_to_end','lag','cpu'):
            result={}
            for row in rows(key):
                sample=[float(v) for t,v in row['values'] if phases[phase]+20<=float(t)<finish and math.isfinite(float(v))]
                if sample: result[row['metric'].get('partition',row['metric'].get('node','all'))]={'mean':statistics.mean(sample),'max':max(sample)}
            stats[str(phase)][key]=result
    (run/'phase-statistics.json').write_text(json.dumps({'phases':phases,'statistics':stats},indent=2),encoding='utf-8')
    print(output/'cache-sharding.png')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('run',type=Path); build(p.parse_args().run)
