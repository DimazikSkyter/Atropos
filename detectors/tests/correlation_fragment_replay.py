"""Requested read_operations fragment: fresh start versus an uninterrupted stream."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import inspect

import numpy as np

from apps.detectors import CorrelationDetector,CorrelationResult
from tests.correlation_change_replay import SETTINGS,MSK,align_results
from tests.influx_correlation_data import CASES,load_case

START='2026-10-18T19:40:00+03:00'
END='2026-10-19T00:10:00+03:00'
CONFIG=dict(SETTINGS['read_operations'])
TRACES=CASES['read_operations']


@dataclass
class FragmentReplay:
    stamps: np.ndarray
    values: np.ndarray
    cold: list[CorrelationResult]
    continued: list[CorrelationResult]
    prefix_stamps: np.ndarray
    prefix_values: np.ndarray
    prefix_results: list[CorrelationResult]
    start_index: int
    settings: dict


def load_fragment():
    stamps,values=load_case('read_operations')
    start=int(np.searchsorted(stamps,datetime.fromisoformat(START).timestamp()))
    stop=int(np.searchsorted(stamps,datetime.fromisoformat(END).timestamp(),side='right'))
    selected_stamps=stamps[start:stop].copy()
    selected_values=values[start:stop].copy()
    if not len(selected_stamps) or selected_stamps[0]!=datetime.fromisoformat(START).timestamp() or selected_stamps[-1]!=datetime.fromisoformat(END).timestamp():
        raise ValueError('InfluxDB does not contain the complete requested fragment')
    cold=align_results(CorrelationDetector(**CONFIG).replay(selected_values),len(selected_values))
    # Both runs end at the same observed input. Neither receives a right halo
    # after END; the final point remains pending in both runs.
    prefix_stamps,prefix_values=stamps[:stop].copy(),values[:stop].copy()
    full=align_results(CorrelationDetector(**CONFIG).replay(prefix_values),len(prefix_values))
    return FragmentReplay(selected_stamps,selected_values,cold,full[start:stop],
                          prefix_stamps,prefix_values,full,start,dict(CONFIG))


def clock(t):
    return datetime.fromtimestamp(int(t),MSK).isoformat()


def run_summary(case,mode):
    rows=case.cold if mode=='cold' else case.continued
    offset=0 if mode=='cold' else case.start_index
    source_stamps=case.stamps if mode=='cold' else case.prefix_stamps
    scored=[i for i,r in enumerate(rows) if r.ready]
    alarms=[i for i in scored if rows[i].detected]
    established=[i for i,r in enumerate(rows) if any(p.monitored for p in r.pairs)]
    peak=max(scored,key=lambda i:rows[i].score)
    pair=max(rows[peak].pairs,key=lambda p:p.score)
    return {'mode':mode,'samples':len(rows),'scored_points':len(scored),
            'pending_points':sum(r.pending for r in rows),
            'provisional_scores':sum(r.ready and r.provisional for r in rows),
            'first_score_target':clock(case.stamps[scored[0]]),
            'first_score_issued':clock(source_stamps[rows[scored[0]].observed_index]),
            'first_established_pair_target':clock(case.stamps[established[0]]) if established else None,
            'first_alarm_target':clock(case.stamps[alarms[0]]) if alarms else None,
            'first_alarm_issued':clock(source_stamps[rows[alarms[0]].observed_index]) if alarms else None,
            'alarm_points':len(alarms),'max_score':rows[peak].score,'peak_target':clock(case.stamps[peak]),
            'peak_pair':[TRACES[i] for i in pair.pair],
            'peak_pair_relative_error':pair.relative_error,
            'peak_pair_alignment_shift':pair.alignment_shift,
            'context_points_before_fragment':offset}


def region_summary(case,mode,start,end):
    rows=case.cold if mode=='cold' else case.continued
    selected=[(t,r) for t,r in zip(case.stamps,rows) if r.ready and
              datetime.fromisoformat(start).timestamp()<=t<datetime.fromisoformat(end).timestamp()]
    return {'scored_points':len(selected),'alarms':sum(r.detected for _,r in selected),
            'max_score':max((r.score for _,r in selected),default=0.)}


def summarize(case):
    regions={'quiet':('2026-10-18T19:40:00+03:00','2026-10-18T21:20:00+03:00'),
             'red_excursion':('2026-10-18T21:30:00+03:00','2026-10-18T22:15:00+03:00'),
             'purple_divergence':('2026-10-18T23:25:00+03:00','2026-10-19T00:11:00+03:00')}
    return {'source':'live InfluxDB query; no CSV fallback','start':START,'end':END,
            'timezone':'Europe/Moscow','traces':list(TRACES),'points_per_trace':len(case.stamps),
            'step_seconds':int(case.stamps[1]-case.stamps[0]),'settings':case.settings,
            'detector_sha256':hashlib.sha256(inspect.getsource(CorrelationDetector).encode()).hexdigest(),
            'runs':{mode:{**run_summary(case,mode),'regions':{
                name:region_summary(case,mode,a,b) for name,(a,b) in regions.items()}}
                for mode in ('cold','continued')}}
