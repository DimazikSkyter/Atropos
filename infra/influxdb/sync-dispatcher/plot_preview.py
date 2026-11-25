"""Inspect both original time extents and the aligned main episode."""
from pathlib import Path
from datetime import datetime,timedelta
import csv,json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

ROOT=Path(__file__).resolve().parent
meta=json.loads((ROOT/'metadata.json').read_text(encoding='utf-8'))
series={}
for r in csv.DictReader((ROOT/'metrics.csv').open(encoding='utf-8')):
    series.setdefault((r['measurement'],r['host']),[]).append((datetime.fromisoformat(r['timestamp']),float(r['value'])))
client='sync_service_client_response_time';dispatch='service_dispatcher_delay'
onset=datetime.fromisoformat(meta['level_jump_time'])
fig,axes=plt.subplots(3,1,figsize=(15,11),layout='constrained')
for ax,metric in zip(axes[:2],[client,dispatch]):
    for (name,host),points in sorted(series.items()):
        if name==metric:ax.plot([t for t,_ in points],[v for _,v in points],color=meta['host_colors'][host],label=host,lw=1.2)
    ax.axvline(onset,color='#333333',ls='--',lw=1,label='Level jump / delay onset')
    ax.set_xlim(*[datetime.fromisoformat(meta['ranges'][metric][k]) for k in ['start','end']])
    ax.legend(ncol=9,fontsize=9,loc='upper left')
axes[0].set(title='Client → synchronous service: main host traces only',ylabel='Response time, ms',ylim=(0,27.5))
axes[1].set(title='Service dispatcher → client: full source time range',ylabel='Dispatcher delay, s',ylim=(0,10))
for (metric,host),points in sorted(series.items()):
    if host=='host1':
        target=axes[2] if metric==client else None
        if target:target.plot([t for t,_ in points],[v for _,v in points],color='#3B6EAF',label='host1 · client response, ms',lw=1.5)
right=axes[2].twinx()
points=series[(dispatch,'host1')]
right.plot([t for t,_ in points],[v for _,v in points],color='#AA9A24',label='host1 · dispatcher delay, s',lw=1.5)
axes[2].axvline(onset,color='#333333',ls='--',lw=1)
axes[2].set(xlim=(onset-timedelta(seconds=45),onset+timedelta(minutes=6,seconds=15)),ylim=(0,27.5),
            ylabel='Client response, ms',title='Aligned episode: dispatcher growth begins with the client response level jump')
right.set(ylabel='Dispatcher delay, s',ylim=(0,10))
lines,labels=axes[2].get_legend_handles_labels();lines2,labels2=right.get_legend_handles_labels()
axes[2].legend(lines+lines2,labels+labels2,loc='upper left',ncol=2,fontsize=9)
for ax in axes:
    ax.grid(alpha=.23);ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=onset.tzinfo))
axes[2].set_xlabel('05 Oct 2026 · Europe/Moscow')
fig.suptitle('Client response / dispatcher delay · approximate screenshot reconstruction',fontsize=17)
fig.savefig(ROOT/'preview.png',dpi=150)
print(ROOT/'preview.png')
