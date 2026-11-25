"""Generate the provisioned dashboard with the existing datasource UID."""
import json
from pathlib import Path

DS = {'type': 'prometheus', 'uid': 'victoria-metrics'}
panels = []
def panel(title, queries, unit='short', description=''):
    i = len(panels)
    panels.append({'id': i + 1, 'type': 'timeseries', 'title': title,
        'description': description, 'datasource': DS,
        'gridPos': {'x': (i % 2) * 12, 'y': (i // 2) * 8, 'w': 12, 'h': 8},
        'targets': [{'refId': chr(65+j), 'expr': q, 'legendFormat': legend,
                     'datasource': DS} for j, (legend, q) in enumerate(queries)],
        'fieldConfig': {'defaults': {'unit': unit, 'min': 0}, 'overrides': []},
        'options': {'legend': {'displayMode': 'table', 'placement': 'bottom',
                    'calcs': ['lastNotNull', 'max']}, 'tooltip': {'mode': 'multi'}}})

panel('01 · Постоянная нагрузка: 750 RPS', [
    ('k6 sent', 'sum(rate(k6_http_reqs_total{experiment="experiment3",name="submit"}[10s]))'),
    ('service1 accepted', 'rate(exp3_received_total{service="service1"}[10s])'),
    ('k6 refused after OOM', 'sum(rate(k6_exp3_load_refused_total{experiment="experiment3"}[10s]))'),
    ('k6 dropped iterations', 'sum(rate(k6_dropped_iterations_total{experiment="experiment3"}[10s]))')], 'reqps')
panel('02 · Деградация сервиса 2: 30–40 → 40–60 ms', [
    ('mean', 'rate(exp3_processing_seconds_sum{service="service2"}[10s]) / rate(exp3_processing_seconds_count{service="service2"}[10s])'),
    ('p95', 'histogram_quantile(0.95, rate(exp3_processing_seconds_bucket{service="service2"}[10s]))'),
    ('timeout 50 ms', '0 * exp3_degraded{service="service2"} + 0.05')], 's',
    'Задержка работы сервиса 2. Deadline сервиса 1 включает HTTP submission и ожидание callback.')
panel('03 · Таймауты и ретраи клиента сервиса 1', [
    ('timeouts', 'rate(exp3_timeouts_total{service="service1"}[10s])'),
    ('retries', 'rate(exp3_retries_total{service="service1"}[10s])'),
    ('failed after 3 attempts', 'rate(exp3_failed_total{service="service1"}[10s])')], 'reqps')
panel('04 · Усиление нагрузки на сервис 2', [
    ('attempts / input', '(rate(exp3_attempts_total{service="service1"}[10s]) / rate(exp3_received_total{service="service1"}[10s])) and (rate(exp3_received_total{service="service1"}[10s]) > 100)'),
    ('retry share', 'rate(exp3_retries_total{service="service1"}[10s]) / clamp_min(rate(exp3_attempts_total{service="service1"}[10s]), 1)')], 'short',
    'Когда пул насыщен, новые попытки остаются в очереди: фактическое усиление ограничено пропускной способностью пула.')
panel('05 · Насыщение клиента: занятые слоты', [
    ('in flight', 'exp3_active{service="service1"}'), ('pool limit', 'exp3_capacity{service="service1"}')])
panel('06 · Неограниченная очередь сервиса 1', [
    ('queued requests incl delayed retries', 'exp3_queue_depth{service="service1"}')], 'short',
    'Каждый запрос сохраняет уникальный payload 4096 bytes. Ретрай переиспользует тот же payload. Активная работа имеет отдельный ограниченный пул.')
panel('07 · Очередь удерживает память → OOM JVM', [
    ('container memory', 'exp3_container_memory_bytes{service="service1"}'),
    ('heap', 'exp3_heap_bytes{service="service1"}'),
    ('JVM heap max (OOM)', 'exp3_heap_max_bytes{service="service1"}'),
    ('memory limit', 'exp3_container_memory_limit_bytes{service="service1"}'),
    ('queue payload lower bound', 'exp3_queue_depth{service="service1"} * exp3_payload_bytes{service="service1"}')], 'bytes')
panel('08 · Сервис 2: ограничение работы и сброс', [
    ('in flight', 'exp3_active{service="service2"}'),
    ('limit', 'exp3_capacity{service="service2"}'),
    ('dropped / second', 'rate(exp3_dropped_total{service="service2"}[10s])')])
panel('09 · HTTP callbacks: поздние результаты', [
    ('callbacks', 'rate(exp3_callbacks_total{service="service1"}[10s])'),
    ('late callbacks', 'rate(exp3_late_callbacks_total{service="service1"}[10s])'),
    ('logical completions', 'rate(exp3_completed_total{service="service1"}[10s])')], 'reqps')
panel('10 · Сервисы: доступность после OOM', [
    ('{{instance}}', 'up{job="experiment3"}'),
    ('degraded phase', 'exp3_degraded{service="service2"}')], 'short',
    'up=0 фиксирует недоступность. Настоящий OOM подтверждается JVM OutOfMemoryError + ExitCode либо Docker OOMKilled в verification.json.')
panel('11 · Память сервиса 2 остаётся ограниченной', [
    ('container memory', 'exp3_container_memory_bytes{service="service2"}'),
    ('memory limit', 'exp3_container_memory_limit_bytes{service="service2"}')], 'bytes')
panel('12 · Возраст головы очереди', [
    ('queue head age', 'exp3_queue_head_age_ms{service="service1"} / 1000')], 's')
panel('13 · GC: частота minor / major', [
    ('{{generation}} GC / sec', 'rate(exp3_gc_events_total{service="service1"}[10s])')], 'ops',
    'Java Parallel GC: Pause Young = minor (PS Scavenge), Pause Full = major (PS MarkSweep). Экспорт из GC log через сервис 2 сохраняет последнюю major сборку после OOM JVM.')
panel('14 · GC: накопленное число сборок', [
    ('{{generation}}', 'exp3_gc_events_total{service="service1"}')], 'short')
panel('15 · GC: время пауз minor / major', [
    ('{{generation}} seconds / sec', 'rate(exp3_gc_pause_seconds_total{service="service1"}[10s])')], 's',
    'Суммарные stop-the-world pause seconds на секунду, не длительность одной сборки.')
panel('16 · Поколения JVM: Eden / Survivor / Old', [
    ('{{pool}}', 'exp3_jvm_memory_pool_bytes{service="service1"}')], 'bytes')
dashboard = {'uid': 'experiment3-retry-oom', 'title': 'Experiment3 · Callback → Timeout → Retry → Queue → OOM',
    'schemaVersion': 39, 'version': 1, 'refresh': '2s', 'tags': ['Atropos', 'experiment3'],
    'timezone': 'browser', 'time': {'from': 'now-15m', 'to': 'now'}, 'panels': panels}
Path(__file__).with_name('dashboard.json').write_text(json.dumps(dashboard, ensure_ascii=False, indent=2), encoding='utf-8')
