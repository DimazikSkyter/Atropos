"""Render reconstructed TPS against the unchanged Cassandra series."""
from pathlib import Path
from datetime import datetime
import csv,json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

ROOT=Path(__file__).resolve().parent
metadata=json.loads((ROOT/'metadata.json').read_text(encoding='utf-8'))
traces={}
for row in csv.DictReader((ROOT/'metrics.csv').open(encoding='utf-8')):
    traces.setdefault(row['trace'],[]).append((datetime.fromisoformat(row['timestamp']),float(row['cassandra_tps'])))
cass={}
for row in csv.DictReader((ROOT.parent/'cassandra-response-time.csv').open(encoding='utf-8')):
    cass.setdefault(row['host'],[]).append((datetime.fromisoformat(row['timestamp']),float(row['cassandra_response_time_ms'])))
colors={'green':'#73BF69','yellow':'#E0B400','gray':'#7B8189','red':'#F2495C','olive':'#9E933F','teal':'#56A89A','orange':'#F48B29'}
fig,axes=plt.subplots(2,1,figsize=(16,9),sharex=True,layout='constrained')
nodecolors=['#5794F2','#FF9830','#F2495C','#B877D9']
for (name,points),color in zip(sorted(cass.items()),nodecolors):
    axes[0].plot([t for t,_ in points],[v for _,v in points],label=name,color=color,lw=1)
for name,points in traces.items():
    axes[1].plot([t for t,_ in points],[v for _,v in points],label=name,color=colors[name],lw=1.1)
labels={'first_node_failure':'Node 1 down: TPS unchanged','node2_plateau':'Node 2 plateau',
        'node3_plateau':'Both plateaus: partial TPS drop','node3_terminal_drop':'Node 3 terminal drop',
        'node2_terminal_drop':'Last terminal drop: TPS = 0'}
for event in metadata['events']:
    t=datetime.fromisoformat(event['time'])
    color='#333333' if event['name']=='first_node_failure' else '#A64048' if 'terminal' in event['name'] else '#357857'
    for ax in axes:ax.axvline(t,color=color,ls='--',lw=.9,alpha=.65)
    if event['name'] in ['first_node_failure','node3_plateau']:
        axes[0].text(t,39,labels[event['name']],rotation=90,va='top',ha='right',fontsize=9,color=color)
axes[0].set(ylim=(0,41),ylabel='Response time, ms',title='Existing Cassandra response time (unchanged)')
axes[1].set(ylim=(0,760),ylabel='TPS',title='TPS digitized from screenshot; time warped to Cassandra events; explicit zero after last terminal drop')
for ax in axes:
    ax.legend(loc='upper left',ncol=7,fontsize=9)
    ax.grid(alpha=.22)
start,end=[datetime.fromisoformat(metadata[k]) for k in ['start','end']]
axes[1].set_xlim(start,end)
axes[1].xaxis.set_major_locator(mdates.HourLocator(interval=1,tz=start.tzinfo))
axes[1].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=start.tzinfo))
axes[1].set_xlabel('01–02 Oct 2026 · Europe/Moscow')
fig.suptitle('Cassandra / TPS · historical reconstruction with aligned events',fontsize=18)
fig.savefig(ROOT/'preview.png',dpi=150)
print(ROOT/'preview.png')
