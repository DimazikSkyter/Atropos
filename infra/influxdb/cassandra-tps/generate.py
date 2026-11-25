"""Align reconstructed TPS to the existing Cassandra replay; Python standard library only."""
from pathlib import Path
import bisect
import csv
from datetime import datetime,timedelta
import json
import statistics

ROOT=Path(__file__).resolve().parent
MEASUREMENT='cassandra_tps'
STEP=15
TRACES=['green','yellow','gray','red','olive','teal','orange']

def load_cassandra():
    series={}
    source=ROOT.parent/'cassandra-response-time.csv'
    for row in csv.DictReader(source.open(encoding='utf-8')):
        series.setdefault(row['host'],[]).append((datetime.fromisoformat(row['timestamp']),float(row['cassandra_response_time_ms'])))
    for points in series.values():points.sort()
    return series

def plateau(points):
    for i,(timestamp,value) in enumerate(points):
        window=[v for _,v in points[i:i+81]]
        if value>=25 and len(window)==81 and statistics.median(window)>=25 and sum(v>=21 for v in window)/81>=.8:
            return timestamp
    raise ValueError('No sustained high plateau found')

def terminal_drop(points):
    index=max(i for i,(_,value) in enumerate(points) if value>=20)
    if index+1>=len(points):raise ValueError('Terminal high plateau did not end')
    return points[index+1][0]

def main():
    cass=load_cassandra()
    first_failure=next(t for i,(t,v) in enumerate(cass['cassandra-1'])
                       if v<.1 and max(x for _,x in cass['cassandra-1'][i:])<.1)
    plateau2,plateau3=plateau(cass['cassandra-2']),plateau(cass['cassandra-3'])
    partial=max(plateau2,plateau3)
    death2,death3=terminal_drop(cass['cassandra-2']),terminal_drop(cass['cassandra-3'])
    death=max(death2,death3)
    start=partial.replace(hour=21,minute=0,second=0,microsecond=0)-timedelta(days=1)
    end=death+timedelta(minutes=15)
    calibration=json.loads((ROOT/'calibration.json').read_text(encoding='utf-8'))
    rows=list(csv.DictReader((ROOT/'reference-traces.csv').open(encoding='utf-8')))
    xs=[float(row['x']) for row in rows]
    values={name:[float(row[name]) for row in rows] for name in TRACES}
    x0,xe,xb=calibration['x_start'],calibration['x_end'],calibration['source_drop_x']
    def value(name,x):
        hi=bisect.bisect_left(xs,x)
        if hi==0:return values[name][0]
        if hi>=len(xs):return values[name][-1]
        lo=hi-1
        fraction=(x-xs[lo])/(xs[hi]-xs[lo])
        return values[name][lo]+fraction*(values[name][hi]-values[name][lo])
    count=0
    with (ROOT/'metrics.lp').open('w',encoding='utf-8',newline='\n') as lp, (ROOT/'metrics.csv').open('w',encoding='utf-8',newline='') as out:
        writer=csv.writer(out)
        writer.writerow(['timestamp','trace','cassandra_tps'])
        for offset in range(0,int((end-start).total_seconds())+1,STEP):
            timestamp=start+timedelta(seconds=offset)
            if timestamp<partial:
                # Map only the pre-drop stroke before this event; no early
                # interpolation across the screenshot's vertical connector.
                x=x0+(xb-1-x0)*(timestamp-start).total_seconds()/(partial-start).total_seconds()
            else:
                # Exclude the blurred vertical connector from the post-drop
                # level, rather than stretching it into a false high plateau.
                x=xb+12+(xe-xb-12)*(timestamp-partial).total_seconds()/(death-partial).total_seconds()
            for name in TRACES:
                measured=0.0 if timestamp>=death else round(value(name,x),3)
                lp.write(f'{MEASUREMENT},trace={name} value={measured:.3f} {int(timestamp.timestamp())}\n')
                writer.writerow([timestamp.isoformat(),name,f'{measured:.3f}'])
                count+=1
    events=[{'name':'first_node_failure','host':'cassandra-1','time':first_failure.isoformat(),'tps_effect':'none'},
            {'name':'node2_plateau','host':'cassandra-2','time':plateau2.isoformat(),'tps_effect':'none_until_both_plateaus'},
            {'name':'node3_plateau','host':'cassandra-3','time':plateau3.isoformat(),'tps_effect':'partial_drop'},
            {'name':'node3_terminal_drop','host':'cassandra-3','time':death3.isoformat(),'tps_effect':'load_remains_positive'},
            {'name':'node2_terminal_drop','host':'cassandra-2','time':death2.isoformat(),'tps_effect':'all_tps_zero'}]
    metadata={'measurement':MEASUREMENT,'field':'value','tag':'trace','unit':'TPS','traces':TRACES,
              'source_measurement':'cassandra_response_time','user_reference_name':'cassandra_compaction_response_time',
              'source_measurement_confirmed_by_user':True,'start':start.isoformat(),'end':end.isoformat(),
              'interval_seconds':STEP,'total_points':count,'points_per_trace':count//len(TRACES),
              'partial_drop_time':partial.isoformat(),'zero_time':death.isoformat(),'events':events,
              'source':'reference.png','source_drop_x':xb,
              'alignment':'Piecewise affine screenshot-time mapping: original pre-drop stroke to start..both-node plateau; '
                          'post-drop noisy positive stroke to plateau..last terminal Cassandra drop. Explicit zeros thereafter.',
              'noise':'Raster fluctuations preserved. No invented noise-change event during response-time growth.',
              'event_detection':'First failure: final run of node1 values<0.1ms. Plateau: first >=25ms sample '
                                 'with next20min median>=25 and >=80% samples>=21ms. Terminal edge: first '
                                 'sample after last>=20ms of each node. These are graph markers, not outage logs.',
              'limitations':calibration['limitations']}
    (ROOT/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(metadata,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
