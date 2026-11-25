"""Corrected Scenario and unchanged Cassandra snapshots shared by ACF/CUSUM."""

from dataclasses import dataclass
from datetime import datetime
import csv
import hashlib
import json

import numpy as np

from tests.trend_replay_appendices import FIXTURES, SNAPSHOT, group_points
from tests.scenario_influx import load_snapshot, LABELS


SETTINGS={
    'default':{'window':256,'max_lag':32,'reference_window':512,'threshold':3.5,'min_change':.1},
    'short':{'window':40,'max_lag':10,'reference_window':160,'threshold':3.5,'min_change':.1},
}


@dataclass(frozen=True)
class ReplayCase:
    name: str
    label: str
    values: np.ndarray
    timestamps: tuple[datetime, ...]
    source: str
    phases: tuple[dict, ...] = ()
    tooth_indices: tuple[int, ...] = ()
    zoom_index: int | None = None


def replay_scenarios():
    groups,metadata=load_snapshot()
    for trace,(times,values) in groups.items():
        phase_times=['2026-10-17T18:11:30+03:00','2026-10-17T20:17:30+03:00',
                     '2026-10-17T22:00:00+03:00','2026-10-17T22:37:00+03:00',
                     '2026-10-17T23:54:30+03:00','2026-10-18T01:55:00+03:00','2026-10-18T04:14:00+03:00']
        bounds=[0,*[int((datetime.fromisoformat(t)-times[0]).total_seconds()/15) for t in phase_times],len(values)]
        labels=['Исходный нулевой уровень','Первое нижнее плато','Первое высокое плато: пики и откаты',
                'Спокойный хвост первого высокого плато','Пауза на нулевом уровне','Второе нижнее плато',
                'Второе высокое плато','Возврат к нулевому уровню']
        phases=tuple({'start':a,'end':z,'label':label} for a,z,label in zip(bounds,bounds[1:],labels))
        a,z=bounds[2]+8,bounds[3]
        delta=np.diff(values[a:z])
        turns=tuple(int(a+i+1) for i in np.flatnonzero(delta[:-1]*delta[1:]<0))
        zoom=a+int(np.argmin(values[a:z]))
        yield ReplayCase('influx-scenario-'+trace,'Сценарий · '+LABELS[trace]+' · TPS',values,times,
                         metadata['source_kind'],phases,turns,zoom)
    path=FIXTURES/(SNAPSHOT+'.csv')
    metadata=json.loads((FIXTURES/(SNAPSHOT+'.json')).read_text(encoding='utf-8'))
    if hashlib.sha256(path.read_bytes()).hexdigest()!=metadata['csv_sha256']:
        raise ValueError('InfluxDB snapshot changed: verify provenance before replay')
    with path.open(encoding='utf-8',newline='') as stream:
        groups=group_points([{'timestamp_utc':r['timestamp_utc'],'host':r['host'],'value_ms':float(r['value_ms'])}
                             for r in csv.DictReader(stream)])
    for host,points in groups.items():
        times=tuple(datetime.fromisoformat(r['timestamp_utc'].replace('Z','+00:00')) for r in points)
        if any((b-a).total_seconds()!=15 for a,b in zip(times,times[1:])):
            raise ValueError('ACF input must preserve the original 15-second grid')
        yield ReplayCase('influx-'+host,'InfluxDB replay: '+host,np.array([r['value_ms'] for r in points]),
                         times,metadata['source_kind'])
