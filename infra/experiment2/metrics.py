"""PromQL shared by dashboard, JSON exports and verification."""
JOB = '{job="experiment2"}'
COUNT = 'sum by (partition) (rate(exp2_processing_seconds_count{job="experiment2"}[20s]))'
SUM = 'sum by (partition) (rate(exp2_processing_seconds_sum{job="experiment2"}[20s]))'
QUERIES = {
    'mean':f'{SUM} / {COUNT}',
    'stddev':f'sqrt(clamp_min(sum by (partition) (rate(exp2_processing_squared_seconds_total{JOB}[20s])) / ({COUNT}) - ({SUM} / {COUNT})^2, 0))',
    'p95':f'histogram_quantile(0.95, sum by (le,partition) (rate(exp2_processing_seconds_bucket{JOB}[20s])))',
    'p99':f'histogram_quantile(0.99, sum by (le,partition) (rate(exp2_processing_seconds_bucket{JOB}[20s])))',
    'hit_ratio':'sum by (partition) (rate(exp2_cache_access_total{job="experiment2",outcome="hit"}[20s])) / sum by (partition) (rate(exp2_cache_access_total{job="experiment2"}[20s]))',
    # Phase counters remain registered. Sum phases within each node before rate so
    # new short-lived phase series don't get a different rate denominator at transitions.
    'produced':f'sum by (partition) (rate((sum by (partition,node) (exp2_produced_total{JOB}))[20s:2s]))',
    'consumed':f'sum by (partition) (rate((sum by (partition,node) (exp2_consumed_total{JOB}))[20s:2s]))',
    'lag':f'max by (partition) (exp2_kafka_lag{JOB})',
    'probe':f'min(exp2_kafka_probe_ok{JOB})',
    'cpu':f'process_cpu_usage{JOB}',
    'db':f'sum by (partition) (rate(exp2_db_seconds_sum{JOB}[20s])) / sum by (partition) (rate(exp2_db_seconds_count{JOB}[20s]))',
    'outcome_mean':f'sum by (partition,outcome) (rate(exp2_processing_seconds_sum{JOB}[20s])) / sum by (partition,outcome) (rate(exp2_processing_seconds_count{JOB}[20s]))',
    'end_to_end':f'sum by (partition) (rate(exp2_end_to_end_seconds_sum{JOB}[20s])) / sum by (partition) (rate(exp2_end_to_end_seconds_count{JOB}[20s]))',
    'api':f'sum by (node) (rate(exp2_api_seconds_sum{{job="experiment2",result="ok"}}[20s])) / sum by (node) (rate(exp2_api_seconds_count{{job="experiment2",result="ok"}}[20s]))',
    'phase_start':f'min by (phase) (exp2_phase_start_seconds{JOB} > 0)',
    'assignments':f'exp2_assigned_partitions{JOB}',
}
