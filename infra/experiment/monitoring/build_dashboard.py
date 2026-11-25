"""Regenerate the checked-in Grafana dashboard; stdlib only."""
import json
from pathlib import Path

panels = []


def panel(title, expressions, unit='s', description=''):
    i = len(panels)
    panels.append({'id':i+1,'title':title,'type':'timeseries','description':description,
        'gridPos':{'x':(i%2)*12,'y':(i//2)*8,'w':12,'h':8},
        'datasource':{'type':'prometheus','uid':'experiment'},
        'targets':[{'refId':chr(65+j),'expr':expr,'legendFormat':legend} for j,(expr,legend) in enumerate(expressions)],
        'fieldConfig':{'defaults':{'unit':unit,'custom':{'drawStyle':'line','lineWidth':2,'fillOpacity':5,'spanNulls':False}},'overrides':[]},
        'options':{'tooltip':{'mode':'multi'},'legend':{'displayMode':'list','placement':'bottom'}}})


panel('01 · Fixed offered load / real accepted and persisted throughput',[
    ('lab_offered_requests_per_second','offered HTTP RPS'),
    ('sum by (stage) (rate(lab_events_total{stage=~"create_received|accepted|persisted"}[20s]))','{{stage}}'),
    ('sum(rate(lab_duration_seconds_count{stage="api_get"}[20s]))','reads')],'ops')
panel('02 · Phase and active faults',[
    ('lab_experiment_phase','phase: 1 baseline / 2 fault / 3 recovery'),
    ('lab_disk_stall_active','disk stall requested'),('lab_network_fault_active','DB network latency')],'short')
panel('03 · API mean and p99 latency',[
    ('sum by (stage,result) (rate(lab_duration_seconds_sum{stage=~"api_.*"}[20s])) / sum by (stage,result) (rate(lab_duration_seconds_count{stage=~"api_.*"}[20s]))','mean {{stage}} {{result}}'),
    ('histogram_quantile(0.99,sum by (le,stage,result) (rate(lab_duration_seconds_bucket{stage=~"api_.*"}[20s])))','p99 {{stage}} {{result}}')])
panel('04 · Asynchronous write: accepted → committed, p50 / p95 / p99',[
    (f'histogram_quantile({q},sum by (le) (rate(lab_duration_seconds_bucket{{stage="end_to_end_write"}}[20s])))',name)
    for q,name in [(.5,'p50'),(.95,'p95'),(.99,'p99')]],description='Only completed writes appear here. Inspect lag and throughput for censored/uncompleted operations.')
panel('05 · Standard deviation of actual durations',[
    ('sqrt(clamp_min(sum by (stage) (rate(lab_duration_squared_seconds_total{result="ok"}[20s])) / sum by (stage) (rate(lab_duration_seconds_count{result="ok"}[20s])) - (sum by (stage) (rate(lab_duration_seconds_sum{result="ok"}[20s])) / sum by (stage) (rate(lab_duration_seconds_count{result="ok"}[20s])))^2, 0))','{{stage}}')])
panel('06 · CPU per application process (1 = one core)',[
    ('rate(process_cpu_seconds_total{job="experiment-app"}[20s])','{{instance}}')],'percentunit')
panel('07 · Kafka publish vs consumer callback (nested API stages)',[
    ('sum by (stage,instance) (rate(lab_duration_seconds_sum{stage=~"kafka_produce|consumer_callback",result="ok"}[20s])) / sum by (stage,instance) (rate(lab_duration_seconds_count{stage=~"kafka_produce|consumer_callback",result="ok"}[20s]))','{{stage}} {{instance}}')])
panel('08 · DB read / write latency by consumer',[
    ('sum by (stage,instance,result) (rate(lab_duration_seconds_sum{stage=~"db_.*"}[20s])) / sum by (stage,instance,result) (rate(lab_duration_seconds_count{stage=~"db_.*"}[20s]))','{{stage}} {{instance}} {{result}}')])
panel('09 · Backlog by partition, independently probed',[
    ('lab_kafka_group_lag','partition {{partition}}')],'short')
panel('10 · Kafka ISR and leader IDs',[
    ('lab_kafka_isr','ISR p{{partition}}'),('lab_kafka_leader','leader p{{partition}}')],'short')
panel('11 · Actual blocked Kafka file operations',[
    ('lab_disk_blocked_calls','currently blocked'),
    ('increase(lab_disk_gate_entries_total[20s])','entered gate / 20s')],'short',
    'Test 1 is rejected if no actual write/flush calls enter the gate. The JVM is not paused.')
panel('12 · Errors / retries and observation availability',[
    ('sum by (stage) (rate(lab_duration_seconds_count{result="error"}[20s]))','errors {{stage}}'),
    ('sum(rate(lab_events_total{stage="db_retry"}[20s]))','DB retries'),
    ('lab_kafka_probe_ok','Kafka probe available'),
    ('min(up{job="experiment-app"})','all app metrics available')],'short')
panel('13 · CPU quota usage across containers',[
    ('sum by (container_label_com_docker_compose_service) (rate(container_cpu_usage_seconds_total{container_label_com_docker_compose_project="atropos-experiment",image!=""}[20s]))','{{container_label_com_docker_compose_service}}')],'percentunit')
panel('14 · Application latency distribution (bucket counts / second)',[
    ('sum by (le,stage) (rate(lab_duration_seconds_bucket{stage=~"api_get|api_post|end_to_end_write",result="ok"}[20s]))','{{stage}} ≤ {{le}} s')],'ops',
    'Cumulative histogram buckets; inspect mean, p99 and standard deviation together.')
dashboard = {'uid':'experiment','title':'Atropos · Kafka disk / PostgreSQL network incidents','schemaVersion':40,
             'version':1,'refresh':'2s','time':{'from':'now-15m','to':'now'},'timezone':'browser',
             'tags':['atropos','experiment'],'panels':panels}
panel('15 · Active HTTP requests by node',[
    ('lab_requests_inflight','{{instance}}')],'short',
    'Idle keep-alive connections are excluded. Compare DB recovery with the remaining API backlog and CPU.')
Path(__file__).with_name('dashboard.json').write_text(json.dumps(dashboard,indent=2),encoding='utf8')
