"""Digitize the highlighted orange trace, retaining 15-second single-point peaks.

The screenshot gives clock labels but no date or y-axis units. Y is therefore
normalized to 0 at idle and 100 at the lower plateau; dates are conventional.
Image antialiasing widens narrow strokes. Each detected narrow excursion is
represented by ONE sample, as specified by the user, using its visible extremum.
"""

import argparse
import csv
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import shutil

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from scipy.ndimage import median_filter
from scipy.signal import find_peaks


NAME='screenshot-step-replay'
MSK=timezone(timedelta(hours=3))


def digitize(source,output):
    output.mkdir(parents=True,exist_ok=True)
    rgb=np.asarray(Image.open(source).convert('RGB'),dtype=float)
    assert rgb.shape[:2]==(1186,2495),'Recalibrate a different image before digitizing it'
    r,g,b=rgb[:,:,0],rgb[:,:,1],rgb[:,:,2]
    # Exclude the red threshold, green/yellow curves, fill, and blue annotation.
    mask=(r>180)&(g>90)&(g<170)&(b<110)&(r>1.45*g)&(r<2.4*g)
    mask[:100]=False;mask[1081:]=False
    low=np.full(rgb.shape[1],np.nan);high=low.copy();middle=low.copy()
    for x in range(rgb.shape[1]):
        ys=np.flatnonzero(mask[:,x])
        if len(ys):
            low[x]=ys.min();high[x]=ys.max();middle[x]=np.median(ys)
    x=np.arange(len(middle))
    for arr in (low,high,middle):
        good=np.isfinite(arr)
        arr[:]=np.interp(x,x[good],arr[good])
    tick_x=np.array([243,546,849,1152,1454,1757,2059,2362],dtype=float)
    tick_seconds=np.arange(len(tick_x))*7200
    pixels_per_second,origin_x=np.polyfit(tick_seconds,tick_x,1)
    zero_y=1070.;medium_y=570.
    unit=100/(zero_y-medium_y)
    # Visible vertical transitions (orange line), manually checked against pixels.
    step_x=[575,893,1245,1440,1744,2095]
    categories=[0,1,2,0,1,2,0]
    bounds=[0,*step_x,len(x)]
    background_pixels=middle.copy()
    events=[]
    for segment,(a,z) in enumerate(zip(bounds,bounds[1:])):
        if categories[segment]==0:
            background_pixels[a:z]=zero_y
            continue
        left=a+7;right=z-7
        local=middle[left:right]
        baseline=median_filter(local,size=61,mode='nearest')
        residual=local-baseline
        detected=[]
        # A clear isolated excursion is at least 8 pixels. Smooth small valleys
        # stay in the background path instead of being replaced by a ramp.
        for sign in (1,-1):
            indices,props=find_peaks(sign*residual,prominence=8,distance=10)
            for k,prominence in zip(indices,props['prominences']):
                px=left+int(k)
                if abs(residual[k])<7:
                    continue
                # Use the visible far end of the vertical stroke, not its median.
                around=slice(max(left,px-3),min(right,px+4))
                tip=float(np.max(high[around]) if sign>0 else np.min(low[around]))
                base=float(baseline[k])
                detected.append((px,sign,tip,base,float(prominence)))
        # The first lower plateau has one deep, single-point downward tooth
        # immediately after its rising edge. It is too close to that edge for
        # the generic interior peak search, so its pixel support is explicit.
        if segment==1:
            detected=[e for e in detected if not 584<=e[0]<=596]
            detected.append((590,1,float(np.max(high[584:595])),float(np.median(middle[598:625])),0.))
            detected.append((891,1,float(np.max(high[888:893])),float(np.median(middle[865:880])),0.))
        clean=middle.copy()
        for px,sign,tip,base,prominence in detected:
            clean[max(a,px-6):min(z,px+7)]=np.nan
            events.append({'source_x':px,'source_y':tip,'background_y':base,'sign':-sign,
                           'prominence_pixels':prominence})
        clean[a:left]=np.nan;clean[right:z]=np.nan
        valid=np.flatnonzero(np.isfinite(clean[a:z]))+a
        background_pixels[a:z]=np.interp(x[a:z],valid,clean[valid])
    first_seconds=int(np.ceil((0-origin_x)/pixels_per_second/15)*15)
    last_seconds=int(np.floor((len(x)-1-origin_x)/pixels_per_second/15)*15)
    seconds=np.arange(first_seconds,last_seconds+1,15,dtype=int)
    point_x=origin_x+seconds*pixels_per_second
    origin=datetime(2026,10,1,16,0,tzinfo=MSK)
    stamps=[origin+timedelta(seconds=int(v)) for v in seconds]
    background=(zero_y-np.interp(point_x,x,background_pixels))*unit
    # Snap every level boundary to a single 15-second slot, avoiding artificial
    # interpolated ramps at a vertical step.
    main_indices=[int(np.argmin(abs(point_x-v))) for v in step_x]
    for idx,px in zip(main_indices,step_x):
        previous=background_pixels[max(0,px-8)]
        following=background_pixels[min(len(x)-1,px+8)]
        near=np.flatnonzero(abs(point_x-px)<8)
        background[near]=np.where(near<idx,(zero_y-previous)*unit,(zero_y-following)*unit)
    values=background.copy()
    accepted=[]
    for event in sorted(events,key=lambda e:e['source_x']):
        i=int(np.argmin(abs(point_x-event['source_x'])))
        if accepted and i-accepted[-1]['index']<2:
            continue
        value=(zero_y-event['source_y'])*unit
        values[i]=value
        accepted.append({**event,'index':i,'timestamp_msk':stamps[i].isoformat(),
                         'value':float(value),'background':float(background[i]),'duration_samples':1})
    pulse_indices={e['index'] for e in accepted}
    pulse_end=max(e['index'] for e in accepted if main_indices[1]<e['index']<main_indices[2])
    # Split off the quiet tail of the first high plateau.
    quiet_start=int(np.searchsorted(seconds,int(np.ceil((seconds[pulse_end]+300)/900)*900)))
    phase_bounds=[0,main_indices[0],main_indices[1],quiet_start,main_indices[2],main_indices[3],main_indices[4],main_indices[5],len(values)]
    labels=['Исходный нулевой уровень','Первое нижнее плато','Первое высокое плато: пики вверх и вниз',
            'Спокойный хвост первого высокого плато','Пауза на нулевом уровне','Второе нижнее плато',
            'Второе высокое плато без зубьев','Возврат к нулевому уровню']
    phases=[{'start':a,'end':z,'label':label} for a,z,label in zip(phase_bounds,phase_bounds[1:],labels)]
    csv_path=output/(NAME+'.csv')
    with csv_path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(['index','timestamp_msk','value','background','is_peak','source_x'])
        for i,(stamp,v,bg,px) in enumerate(zip(stamps,values,background,point_x)):
            writer.writerow([i,stamp.isoformat(),float(v),float(bg),i in pulse_indices,float(px)])
    image_path=output/(NAME+'-reference.png')
    if source.resolve()!=image_path.resolve():
        shutil.copy2(source,image_path)
    metadata={'schema_version':2,'source_image':image_path.name,'source_image_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
              'csv_sha256':hashlib.sha256(csv_path.read_bytes()).hexdigest(),
              'value_sha256':hashlib.sha256(np.asarray(values,dtype='<f8').tobytes()).hexdigest(),
              'source_size':[2495,1186],'trace':'Highlighted upper orange line',
              'date':'2026-11-04','interval_seconds':15,'points':len(values),
              'start_msk':stamps[0].isoformat(),'end_msk':stamps[-1].isoformat(),
              'time_calibration':{'tick_pixels':tick_x.tolist(),'tick_labels':['16:00','18:00','20:00','22:00','00:00','02:00','04:00','06:00'],
                                  'pixels_per_second':float(pixels_per_second),'pixel_at_16_00':float(origin_x)},
              'y_calibration':{'zero_pixel_y':zero_y,'lower_plateau_pixel_y':medium_y,'lower_plateau_relative_value':100},
              'main_steps':[{'index':i,'source_x':px,'timestamp_msk':stamps[i].isoformat()} for i,px in zip(main_indices,step_x)],
              'peaks':accepted,'phases':phases,
              'limitations':['Y units are not shown: values are relative; lower plateau is 100.',
                             'Only clock labels are visible; the dates 1--2 October are conventional.',
                             'Step/peak times are estimated from pixels and rounded to a 15-second grid.',
                             'Each narrow peak is one sample by explicit user instruction, not inferred stroke width.',
                             'Subpixel background values are linearly interpolated; this can introduce short-lag correlation.']}
    (output/(NAME+'.json')).write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    return metadata,values,stamps,point_x


def comparison(source,output,metadata,values,stamps,point_x):
    image=Image.open(source)
    fig,axes=plt.subplots(2,1,figsize=(16,12),layout='constrained')
    axes[0].imshow(image,aspect='auto');axes[0].axis('off')
    axes[0].set_title('Присланный график: источник без изменений',fontsize=13)
    axes[1].set_facecolor('#181b20')
    axes[1].plot(point_x,values,color='#f59e0b',lw=.9,drawstyle='steps-post')
    axes[1].set_xlim(0,2494);axes[1].set_ylim(-3,190)
    axes[1].set_xticks(metadata['time_calibration']['tick_pixels'],metadata['time_calibration']['tick_labels'])
    axes[1].set_ylabel('Относительные единицы');axes[1].set_xlabel('Время со скриншота; шаг 15 секунд')
    axes[1].grid(alpha=.2)
    axes[1].set_title('Оцифрованная верхняя оранжевая линия: оба ступенчатых эпизода',fontsize=13)
    fig.savefig(output/(NAME+'-comparison.png'),dpi=110);plt.close(fig)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('--output',type=Path,default=Path(__file__).with_name('fixtures'))
    args=parser.parse_args()
    meta,values,times,px=digitize(args.source,args.output)
    comparison(args.source,args.output,meta,values,times,px)
    print(json.dumps({k:meta[k] for k in ('points','start_msk','end_msk','main_steps','peaks')},ensure_ascii=False,indent=2))
