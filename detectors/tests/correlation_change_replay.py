"""Continuous online replay, from the first point queried from Atropos InfluxDB."""
from datetime import datetime, timedelta, timezone

from apps.detectors import CorrelationDetector,CorrelationResult
from tests.influx_correlation_data import CASES, load_case

MSK = timezone(timedelta(hours=3))
COMMON = {'adaptive': True, 'min_samples':5, 'min_change':.25,
          'min_correlation':.7, 'alert_threshold':.4, 'relative_noise_floor':.3,
          'noise_floor':0., 'persistence':2,'alignment_tolerance':1,'relative_change_scale':.1}
SETTINGS = {
    'latency': {**COMMON, 'window':12, 'max_lag':2, 'reference_window':64},
    'read_operations': {**COMMON, 'window':32, 'max_lag':4, 'reference_window':128},
}


def align_results(issued,count):
    """Plot delayed outputs at their scored timestamps, leaving the final halo pending."""
    rows=[CorrelationResult(score_index=i,pending=True) for i in range(count)]
    for result in issued:
        if result.score_index is not None and 0<=result.score_index<count:
            rows[result.score_index]=result
    return rows


def replay(case):
    stamps, values = load_case(case)
    results = align_results(CorrelationDetector(**SETTINGS[case]).replay(values),len(values))
    return stamps, values, 0, results


def summary(case, stamps, split, results):
    alerts = [(int(t),r) for t,r in zip(stamps,results) if r.detected]
    first_ready = next((i for i,r in enumerate(results) if r.ready),None)
    last_ready=next((r for r in reversed(results) if r.ready),None)
    def clock(t):
        return datetime.fromtimestamp(int(t),MSK).isoformat()
    return {'scenario':case,'source':'live InfluxDB query; continuous online replay',
            'points_per_trace':len(stamps),'traces':list(CASES[case]),
            'step_seconds':int(stamps[1]-stamps[0]), 'manual_reference':False,
            'settings':SETTINGS[case], 'first_score':clock(stamps[first_ready]) if first_ready is not None else None,
            'scored_observations':sum(r.ready for r in results),
            'provisional_scores':sum(r.ready and r.provisional for r in results),
            'pending_points':sum(r.pending for r in results),
            'alignment_delay_seconds':int(stamps[1]-stamps[0])*SETTINGS[case]['alignment_tolerance'],
            'first_score_issued':clock(stamps[results[first_ready].observed_index]) if first_ready is not None else None,
            'first_alarm':clock(alerts[0][0]) if alerts else None,
            'first_alarm_issued':clock(stamps[alerts[0][1].observed_index]) if alerts else None,
            'max_score':max((r.score for r in results),default=0),
            'alarmed_points':len(alerts),
            'final_reference_pairs':[{'pair':[CASES[case][i] for i in p.pair],
                                'lag':p.reference_lag,'r':p.reference_correlation}
                               for p in (last_ready.pairs if last_ready else ()) if p.monitored]}
