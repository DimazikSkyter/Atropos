"""Resample the two calibrated historical graphs without extrapolating either one."""
from pathlib import Path
import argparse,bisect,csv,json,math
from datetime import datetime,timedelta,timezone

ROOT=Path(__file__).resolve().parent
CLIENT='sync_service_client_response_time'; DISPATCHER='service_dispatcher_delay'
STEP=5;MSK=timezone(timedelta(hours=3))

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--date',default='2026-10-05');args=parser.parse_args()
    calibration=json.loads((ROOT/'calibration.json').read_text(encoding='utf-8'))
    def stamp(t):return datetime.fromisoformat(args.date+'T'+t).replace(tzinfo=MSK)
    baseline_start=stamp('18:15:00');baseline_end=stamp(calibration['dispatcher_growth_anchor'])
    series={}
    for row in csv.DictReader((ROOT/'reference-knots.csv').open(encoding='utf-8')):
        series.setdefault((row['measurement'],row['host']),[]).append((stamp(row['time']),float(row['value'])))
    counts={};ranges={}
    with (ROOT/'metrics.lp').open('w',encoding='utf-8',newline='\n') as lp,(ROOT/'metrics.csv').open('w',encoding='utf-8',newline='') as csvout:
        writer=csv.writer(csvout);writer.writerow(['timestamp','measurement','host','value','unit'])
        for (metric,host),points in sorted(series.items()):
            points.sort();times=[t.timestamp() for t,_ in points];values=[v for _,v in points]
            begin,end=points[0][0],points[-1][0]
            ranges[metric]={'start':begin.isoformat(),'end':end.isoformat()}
            count=0
            for offset in range(0,int((end-begin).total_seconds())+1,STEP):
                t=begin+timedelta(seconds=offset);ts=t.timestamp();hi=bisect.bisect_left(times,ts)
                if hi==0:value=values[0]
                elif hi==len(times):value=values[-1]
                else:
                    lo=hi-1;f=(ts-times[lo])/(times[hi]-times[lo]);value=values[lo]+f*(values[hi]-values[lo])
                if metric==DISPATCHER and baseline_start<=t<=baseline_end:
                    sample=(t-baseline_start).total_seconds()/STEP;phase=int(host.removeprefix('host'))
                    value=.015+.003*math.sin(sample*.73+phase)+.002*math.sin(sample*.19+phase*1.7)
                value=round(value,6)
                lp.write(f'{metric},host={host} value={value:.6f} {int(ts)}\n')
                writer.writerow([t.isoformat(),metric,host,f'{value:.6f}','ms' if metric==CLIENT else 's'])
                count+=1
            counts.setdefault(metric,{})[host]=count
    metadata={'date':args.date,'timezone':'Europe/Moscow','interval_seconds':STEP,'ranges':ranges,'points_per_series':counts,
              'total_points':sum(sum(c.values()) for c in counts.values()),'host_colors':calibration['host_colors'],
              'measurement_labels':{CLIENT:'ВО клиента к синхронному сервису',DISPATCHER:'Задержка диспетчера сервиса, работающего с клиентом'},
              'units':{CLIENT:'ms',DISPATCHER:'s'},'level_jump_time':stamp(calibration['level_jump_time']).isoformat(),
              'dispatcher_growth_anchor':stamp(calibration['dispatcher_growth_anchor']).isoformat(),
              'dispatcher_baseline':{'start':baseline_start.isoformat(),'end':baseline_end.isoformat(),
                                     'min':.010,'max':.020,'unit':'s','method':'User-requested deterministic baseline before growth'},
              'client_only_main_band':True,'client_after_18_25':'No points; no extrapolated recovery or zero fill',
              'limitations':calibration['limitations']}
    (ROOT/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'total_points':metadata['total_points'],'points_per_series':counts},indent=2))

if __name__=='__main__':main()
