"""Read only the selected fields/queries and window from their original sources."""
import math
from decimal import Decimal

from atropos_ui.clients import ApiError, PrometheusClient
from atropos_ui.influx import read_window
from atropos_ui.resource_limits import limit


def read_metric_window(source, start_ns, end_ns, metrics, *, step=None, transport=None):
    if source.kind == 'InfluxDB':
        return read_window(source, start_ns, end_ns, metrics, transport=transport)
    if not source.prometheus_compatible:
        raise ValueError('Для расчёта поддерживаются Prometheus, VictoriaMetrics и InfluxDB 2.x.')
    interval = step if step is not None else max(30, source.min_interval_seconds)
    client = PrometheusClient(source.url, datasource=source, transport=transport)
    result, skipped, count, series_count = {}, 0, 0, 0
    for metric in metrics:
        if metric.get('kind') != 'promql':
            raise ValueError('У метрики отсутствует сохранённый PromQL-запрос.')
        data = client.range(metric['query'], start_ns / 1e9, end_ns / 1e9, interval)
        if data.get('resultType') != 'matrix':
            raise ApiError('Для детекторов ожидаются числовые временные ряды.')
        series = []
        for row in data['result']:
            if 'histograms' in row:
                raise ApiError('Для детекторов преобразуйте histogram в числовые значения в PromQL.')
            times, values = [], []
            for timestamp, value in row.get('values', []):
                stamp = int(Decimal(str(timestamp)) * 1_000_000_000)
                number = float(value)
                if not math.isfinite(number):
                    skipped += 1
                    continue
                if start_ns <= stamp < end_ns:
                    times.append(stamp)
                    values.append(number)
            if times:
                if times != sorted(set(times)):
                    raise ApiError('Источник вернул повторные или неупорядоченные timestamps.')
                series.append({'labels': {k: v for k, v in row['metric'].items() if k != '__name__'},
                               'times': times, 'values': values})
                count += len(times)
                series_count += 1
                if count > limit('IMPORT_MAX_POINTS', 500_000) or series_count > limit('IMPORT_MAX_SERIES', 10_000):
                    raise ValueError('Окно превышает лимит точек или рядов. Сократите период или сет.')
        result[metric['id']] = {**metric, 'series': series}
    return result, skipped
