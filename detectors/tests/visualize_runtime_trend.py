"""Detailed causal trend report, PNG/CSV/JSON/HTML, with explicit decision reasons."""

import argparse
import csv
from dataclasses import asdict
import hashlib
import html
import json
import math
from pathlib import Path
import platform
import re
from statistics import median

import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
import numpy as np

from apps.runtime_trend import RuntimeTrendDetector, _fit
from tests.runtime_trend_scenarios import report_scenarios


REASONS = {'warmup': 'Прогрев', 'flat': 'Малый наклон', 'variance': 'Шум выше порога',
           'step': 'Ступенька', 'confirmation': 'Ждёт подтверждения', 'trend': 'Тренд'}
COLORS = {'warmup': '#94a3b8', 'flat': '#64748b', 'variance': '#f59e0b',
          'step': '#8b5cf6', 'confirmation': '#ec4899', 'trend': '#16a34a'}


def expected_context(start, end, trends):
    """Label the analysed window, so the tail of a trend is not a false alarm."""
    if trends is None:
        return None
    directions = {span.direction for span in trends if span.start < end and span.end > start}
    return '|'.join(sorted(directions)) or 'steady'


def replay_scenario(case):
    if not case.points or not math.isfinite(case.evaluation_seconds) or case.evaluation_seconds <= 0:
        raise ValueError('require points and a finite positive evaluation cadence')
    detector = RuntimeTrendDetector(case.config)
    next_due = case.points[0][0]
    samples, evaluations = [], []
    for index, (timestamp, value) in enumerate(case.points):
        detector.update(timestamp, value)
        due = timestamp >= next_due
        evaluated = due or index == len(case.points)-1
        sample = {'index': index, 'timestamp_seconds': timestamp, 'value': value, 'evaluated': evaluated}
        if evaluated:
            result = detector.evaluate()
            row = asdict(result)
            # Read diagnostic state only: no extra evaluate(), hence no extra confirmation.
            start = detector._points[0][0]
            row.update(evaluation_kind='scheduled' if due else 'final', window_start_seconds=start,
                       window_span_seconds=timestamp-start,
                       coverage=(timestamp-start)/case.config.window_seconds,
                       tangent_pass=abs(result.tangent) > case.config.min_tangent if result.ready else None,
                       variance_pass=result.residual_variance <= case.config.variance_threshold if result.ready else None,
                       expected_context=expected_context(start, timestamp, case.trends))
            evaluations.append(row)
            sample.update({key: value for key, value in row.items() if key != 'timestamp'})
            if due:
                next_due += (math.floor((timestamp-next_due)/case.evaluation_seconds)+1)*case.evaluation_seconds
        samples.append(sample)
    return samples, evaluations


def summarize(case, evaluations, stem):
    ready = [r for r in evaluations if r['ready']]
    alarms = [r for r in evaluations if r['detected']]
    directions = {r['direction'] for r in alarms}
    checks = {'no_alarm': bool(ready) and not alarms, 'up': 'up' in directions,
              'both': {'up', 'down'} <= directions, 'false_alarms': bool(alarms)}
    runs, previous = 0, None
    for row in evaluations:
        if row['detected'] and (previous is None or not previous['detected']
                                or row['direction'] != previous['direction']):
            runs += 1
        previous = row
    reactions = []
    for span in case.trends or ():
        matching = [r for r in alarms if r['direction'] == span.direction
                    and r['timestamp'] >= span.start and r['window_start_seconds'] < span.end]
        first = matching[0]['timestamp'] if matching else None
        reactions.append({**asdict(span), 'first_matching_alarm_seconds': first,
                          'delay_seconds': first-span.start if first is not None else None})
    outside = None if case.trends is None else sum(r['expected_context'] == 'steady' for r in alarms)
    if case.slug == 'response-quiet_rise':
        delay = reactions[0]['delay_seconds']
        checks['up'] = checks['up'] and delay is not None and 0 <= delay <= 120
    selected = max(ready, key=lambda r: abs(r['tangent'])) if ready else evaluations[-1]
    return {'stem': stem, 'slug': case.slug, 'name': case.name, 'label': case.label,
            'source': case.source, 'expectation': case.expectation, 'criterion': case.criterion,
            'criterion_met': checks.get(case.criterion), 'unit': case.unit,
            'config': asdict(case.config), 'points': len(case.points),
            'evaluation_seconds': case.evaluation_seconds, 'evaluations': len(evaluations),
            'final_evaluations': sum(r['evaluation_kind'] == 'final' for r in evaluations),
            'ready_evaluations': len(ready), 'warmup_evaluations': len(evaluations)-len(ready),
            'alarm_evaluations': len(alarms), 'up_evaluations': sum(r['direction']=='up' for r in alarms),
            'down_evaluations': sum(r['direction']=='down' for r in alarms), 'alarm_runs': runs,
            'alarm_fraction_ready': len(alarms)/len(ready) if ready else None,
            'first_ready_seconds': ready[0]['timestamp'] if ready else None,
            'first_alarm_seconds': alarms[0]['timestamp'] if alarms else None,
            'alarms_outside_labelled_trend_windows': outside,
            'max_abs_tangent': max(abs(r['tangent']) for r in ready) if ready else None,
            'max_residual_variance': max(r['residual_variance'] for r in ready) if ready else None,
            'reason_counts': {reason: sum(r['reason']==reason for r in evaluations) for reason in REASONS},
            'events': [asdict(s) for s in case.events], 'trend_reactions': reactions,
            'selected_window': dict(selected)}


def window_diagnostics(case, selected):
    now = selected['timestamp']
    points = [(t, v) for t, v in case.points if selected['window_start_seconds'] <= t <= now]
    points = points[-case.config.max_samples:]
    assert len(points) == selected['samples']
    x = [(t-now)/60 for t, _ in points]
    y = [value/case.config.value_scale for _, value in points]
    if not selected['ready']:
        return [{'timestamp_seconds': t, 'relative_minutes': rel, 'value': value,
                 'fitted_value': None, 'residual_normalized': None,
                 'winsorized_residual_normalized': None, 'cap_normalized': None}
                for (t, value), rel in zip(points, x)]
    intercept, tangent, variance, cleaned = _fit(x, y, case.config)
    assert math.isclose(tangent, selected['tangent'], rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(variance, selected['residual_variance'], rel_tol=1e-12, abs_tol=1e-12)
    fitted = [intercept+tangent*t for t in x]
    residuals = [value-fit for value, fit in zip(y, fitted)]
    center = median(residuals)
    sigma = max(case.config.noise_floor, 1.4826*median(abs(r-center) for r in residuals))
    cap = case.config.huber_delta*sigma
    return [{'timestamp_seconds': t, 'relative_minutes': rel, 'value': value,
             'fitted_value': fit*case.config.value_scale, 'residual_normalized': residual,
             'winsorized_residual_normalized': clean-fit, 'cap_normalized': cap}
            for (t, value), rel, fit, residual, clean in zip(points, x, fitted, residuals, cleaned)]


def save_csv(path, rows, fields=None):
    fields = fields or list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', encoding='utf-8', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def render_case(output, case, evaluations, info, window):
    times = np.array([t/60 for t, _ in case.points])
    values = np.array([v for _, v in case.points])
    t = np.array([r['timestamp']/60 for r in evaluations])
    tangent = np.array([r['tangent'] if r['ready'] else np.nan for r in evaluations])
    variance = np.array([r['residual_variance'] if r['ready'] else np.nan for r in evaluations])
    selected = info['selected_window']
    fig, axes = plt.subplots(4, 1, figsize=(13, 10), sharex=True, layout='constrained',
                             gridspec_kw={'height_ratios': (3, 1.5, 1.5, 1.7)})
    fig.suptitle(f'{case.label}\nОкно {case.config.window_seconds/60:g} мин; '
                 f'|наклон| > {case.config.min_tangent:g}; остатки ≤ {case.config.variance_threshold:g}', fontsize=12)
    axes[0].plot(times, values, color='#334155', linewidth=0.8, label='Исходный ряд')
    for direction, color, marker, label in (('up', '#dc2626', '^', 'Обнаружен рост'),
                                           ('down', '#0891b2', 'v', 'Обнаружено падение')):
        alarms = [r for r in evaluations if r['detected'] and r['direction'] == direction]
        if alarms:
            alarm_t = np.array([r['timestamp']/60 for r in alarms])
            axes[0].scatter(alarm_t, np.interp(alarm_t, times, values), s=22, marker=marker,
                            color=color, zorder=4, label=label)
    if selected['ready']:
        axes[0].plot([r['timestamp_seconds']/60 for r in window], [r['fitted_value'] for r in window],
                     color='#7c3aed', linewidth=2, label='Линия выбранного окна')
    axes[0].set_ylabel(case.unit)
    axes[0].legend(fontsize=8, ncol=2)
    axes[1].plot(t, tangent, color='#2563eb', linewidth=1)
    axes[1].axhline(case.config.min_tangent, color='#dc2626', linestyle='--', linewidth=0.8)
    axes[1].axhline(-case.config.min_tangent, color='#0891b2', linestyle='--', linewidth=0.8)
    axes[1].axhline(0, color='#94a3b8', linewidth=0.5)
    axes[1].set_ylabel('Наклон\nнорм. ед./мин')
    axes[2].plot(t, variance, color='#d97706')
    axes[2].axhline(case.config.variance_threshold, color='#dc2626', linestyle='--', linewidth=0.8)
    axes[2].set_yscale('symlog', linthresh=max(case.config.noise_floor**2,
                                            case.config.variance_threshold*0.001, 1e-12))
    axes[2].set_ylabel('Остаточный момент\n(symlog)')
    state_labels = ['Прогрев', 'Малый наклон', 'Шум', 'Ступенька', 'Подтверждение', 'Рост', 'Падение']
    state_codes = {'warmup': 0, 'flat': 1, 'variance': 2, 'step': 3, 'confirmation': 4}
    codes = [state_codes.get(r['reason'], 5 if r['direction']=='up' else 6) for r in evaluations]
    axes[3].scatter(t, codes, c=[COLORS[r['reason']] for r in evaluations], s=11)
    axes[3].set_yticks(range(7), state_labels, fontsize=8)
    axes[3].set_ylim(-0.6, 6.6)
    axes[3].set_ylabel('Решение')
    axes[3].set_xlabel('Время, минуты; отметки соответствуют фактическим evaluate()')
    first_ready = info['first_ready_seconds']
    for ax in axes:
        ax.axvspan(times[0], first_ready/60 if first_ready is not None else times[-1],
                   color='#94a3b8', alpha=0.12)
        for span in case.events:
            ax.axvspan(span.start/60, min(span.end/60, times[-1]), color='#f59e0b', alpha=0.09)
        ax.axvspan(selected['window_start_seconds']/60, selected['timestamp']/60,
                   color='#7c3aed', alpha=0.07)
        ax.grid(alpha=0.18)
    fig.savefig(output/f'{info["stem"]}.png', dpi=115)
    plt.close(fig)
    fig, axes = plt.subplots(2, 1, figsize=(12, 6), sharex=True, layout='constrained')
    fig.suptitle(f'Разбор окна с максимальным |наклоном|: t={selected["timestamp"]/60:g} мин\n'
                 f'{REASONS[selected["reason"]]}; наклон={selected["tangent"]:.5g}; '
                 f'остаточный момент={selected["residual_variance"]:.5g}', fontsize=11)
    rel = [r['relative_minutes'] for r in window]
    axes[0].plot(rel, [r['value'] for r in window], 'o-', color='#334155', markersize=3, label='Наблюдение')
    if selected['ready']:
        fitted = np.array([r['fitted_value'] for r in window])
        cap = window[0]['cap_normalized']
        axes[0].plot(rel, fitted, color='#7c3aed', label='Робастная линия')
        axes[0].fill_between(rel, fitted-cap*case.config.value_scale,
                            fitted+cap*case.config.value_scale, color='#7c3aed', alpha=0.12,
                            label='Предел ограничения остатков')
        axes[1].plot(rel, [r['residual_normalized'] for r in window], 'o-', markersize=3,
                     color='#94a3b8', label='Исходные остатки')
        axes[1].plot(rel, [r['winsorized_residual_normalized'] for r in window], 'o-', markersize=3,
                     color='#d97706', label='Ограниченные остатки')
        axes[1].axhline(cap, color='#7c3aed', linestyle='--', linewidth=0.8)
        axes[1].axhline(-cap, color='#7c3aed', linestyle='--', linewidth=0.8)
        axes[1].legend(fontsize=8)
    axes[0].legend(fontsize=8)
    axes[0].set_ylabel(case.unit)
    axes[1].set_ylabel('Остаток, норм. ед.')
    axes[1].set_xlabel('Минуты до момента оценки; 0 — evaluate()')
    for ax in axes:
        ax.grid(alpha=0.18)
    fig.savefig(output/f'{info["stem"]}-window.png', dpi=115)
    plt.close(fig)


def load_test_run(path):
    if path is None:
        return None, None
    log = Path(path).read_text(encoding='utf-8')
    match = re.search(r'Ran (\d+) tests in ([\d.]+)s', log)
    if not match or not re.search(r'^OK(?: \(skipped=\d+\))?$', log, re.MULTILINE):
        raise ValueError('test log must contain a successful complete unittest run')
    skipped = re.search(r'^OK \(skipped=(\d+)\)', log, re.MULTILINE)
    return {'total': int(match[1]), 'seconds': float(match[2]),
            'skipped': int(skipped[1]) if skipped else 0, 'status': 'OK'}, log


def create_report(output, scenarios=None, test_log=None):
    test_run, log = load_test_run(test_log)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cases = list(report_scenarios() if scenarios is None else scenarios)
    if not cases:
        raise ValueError('report requires at least one scenario')
    summary = []
    for index, case in enumerate(cases, 1):
        samples, evaluations = replay_scenario(case)
        stem = f'{index:02d}'
        info = summarize(case, evaluations, stem)
        window = window_diagnostics(case, info['selected_window'])
        save_csv(output/f'{stem}-samples.csv', [{'timestamp_seconds': t, 'value': v} for t, v in case.points])
        save_csv(output/f'{stem}-points.csv', samples)
        save_csv(output/f'{stem}-detections.csv', evaluations)
        save_csv(output/f'{stem}-window.csv', window)
        render_case(output, case, evaluations, info, window)
        summary.append(info)
    detector_path = Path(__file__).resolve().parents[1]/'apps/runtime_trend.py'
    report = {'schema_version': 2, 'report_date': '2026-11-03',
              'python': platform.python_version(), 'numpy': np.__version__, 'matplotlib': matplotlib.__version__,
              'detector_source_sha256': hashlib.sha256(detector_path.read_bytes()).hexdigest(),
              'scenario_count': len(summary), 'test_run': test_run, 'scenarios': summary}
    (output/'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    flat = [{k: v for k, v in row.items() if not isinstance(v, (dict, list))} for row in summary]
    save_csv(output/'summary.csv', flat)
    reason_rows = [{'slug': r['slug'], **r['reason_counts']} for r in summary]
    save_csv(output/'reasons.csv', reason_rows)
    if log is not None:
        (output/'test-results.txt').write_text(log, encoding='utf-8')
    _overview(output, summary)
    _html(output, report)
    return report


def _overview(output, rows):
    fig, axes = plt.subplots(1, 2, figsize=(15, max(5, len(rows)*0.32)), sharey=True, layout='constrained')
    y = np.arange(len(rows))
    up, down = [r['up_evaluations'] for r in rows], [r['down_evaluations'] for r in rows]
    axes[0].barh(y, up, color='#dc2626', label='Рост')
    axes[0].barh(y, down, left=up, color='#0891b2', label='Падение')
    axes[0].set_yticks(y, [f'{r["stem"]} {r["label"][:55]}' for r in rows], fontsize=8)
    axes[0].invert_yaxis()
    axes[0].set_title('Оценки с обнаруженным трендом')
    axes[0].legend(fontsize=8)
    left = np.zeros(len(rows))
    for reason in REASONS:
        counts = [r['reason_counts'][reason] for r in rows]
        axes[1].barh(y, counts, left=left, color=COLORS[reason], label=REASONS[reason])
        left += counts
    axes[1].set_title('Все решения, включая прогрев и отказы')
    axes[1].legend(fontsize=8, ncol=2)
    for ax in axes:
        ax.set_xlabel('Число оценок, не число независимых инцидентов')
        ax.grid(axis='x', alpha=0.2)
    fig.savefig(output/'overview.png', dpi=115)
    plt.close(fig)


def _html(output, report):
    rows = report['scenarios']
    legacy_count = sum(r['source'].startswith('tests/scenarios.py') for r in rows)
    bond_count = sum(r['source'].startswith('tests/prediction_interval_scenarios.py') for r in rows)
    def number(value, unit=''):
        return '—' if value is None else f'{value:g}{unit}'
    table, sections = [], []
    for row in rows:
        outcome = 'наблюдение' if row['criterion_met'] is None else ('выполнен' if row['criterion_met'] else 'НЕ выполнен')
        table.append(f'<tr><td><a href="#{row["slug"]}">{row["stem"]}. {html.escape(row["label"])}</a></td>'
                     f'<td>{row["config"]["window_seconds"]/60:g}</td><td>{row["ready_evaluations"]}/{row["evaluations"]}</td>'
                     f'<td>{row["up_evaluations"]}/{row["down_evaluations"]}</td>'
                     f'<td>{number(100*row["alarm_fraction_ready"] if row["alarm_fraction_ready"] is not None else None, "%")}</td>'
                     f'<td>{number(row["first_alarm_seconds"]/60 if row["first_alarm_seconds"] is not None else None)}</td>'
                     f'<td>{number(row["alarms_outside_labelled_trend_windows"])}</td><td>{outcome}</td></tr>')
        reasons = ' · '.join(f'{REASONS[k]}: {v}' for k, v in row['reason_counts'].items())
        reactions = ''.join('<li>'+html.escape(r['label'])+': задержка '+number(r['delay_seconds'], ' с')+'</li>'
                            for r in row['trend_reactions'])
        config = html.escape(json.dumps(row['config'], ensure_ascii=False, indent=2))
        selected = row['selected_window']
        sections.append(f'<section id="{row["slug"]}"><h2>{row["stem"]}. {html.escape(row["label"])}</h2>'
                        f'<p><b>Ожидание:</b> {html.escape(row["expectation"])}</p>'
                        f'<p>Источник: <code>{html.escape(row["source"])}</code>. Точек: {row["points"]}; '
                        f'оценок: {row["evaluations"]}, из них готовых: {row["ready_evaluations"]}; '
                        f'серий последовательных тревог: {row["alarm_runs"]}. Критерий: {outcome}.</p>'
                        f'<p>Max |наклон|: {number(row["max_abs_tangent"])} норм. ед./мин; '
                        f'max остаточный момент: {number(row["max_residual_variance"])}. '
                        f'Первая готовая оценка: {number(row["first_ready_seconds"]/60 if row["first_ready_seconds"] is not None else None, " мин")}.</p>'
                        f'<p>{reasons}</p><ul>{reactions}</ul>'
                        f'<img src="{row["stem"]}.png" loading="lazy" alt="{html.escape(row["label"], quote=True)}">'
                        f'<details><summary>Разбор выбранного окна: t={selected["timestamp"]/60:g} мин, '
                        f'{REASONS[selected["reason"]]}</summary>'
                        f'<img src="{row["stem"]}-window.png" loading="lazy" alt="Робастная линия и остатки">'
                        '<p>Полоса — предел ограничения остатков Huber/MAD при подгонке линии. '
                        'Она не является доверительным или предиктивным интервалом.</p></details>'
                        f'<details><summary>Все параметры</summary><pre>{config}</pre></details>'
                        f'<p><a href="{row["stem"]}-points.csv">Все точки и фактические оценки</a> · '
                        f'<a href="{row["stem"]}-detections.csv">Оценки и причины CSV</a> · '
                        f'<a href="{row["stem"]}-samples.csv">Исходный ряд CSV</a> · '
                        f'<a href="{row["stem"]}-window.csv">Выбранное окно CSV</a></p></section>')
    test = report['test_run']
    test_text = (f'<p><b>Тесты: {test["total"]-test["skipped"]} успешных, {test["skipped"]} пропущен.</b> '
                 '<a href="test-results.txt">Полный журнал</a></p>' if test else '')
    page = f'''<!doctype html><html lang="ru"><meta charset="utf-8">
<title>RuntimeTrendDetector — подробный отчёт</title>
<style>body{{font:16px/1.55 system-ui;color:#1e293b;max-width:1400px;margin:32px auto;padding:0 20px}}
img{{width:100%;height:auto}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:7px;border-bottom:1px solid #ddd;text-align:left}}
th{{background:#f1f5f9}}section{{margin:48px 0;border-top:1px solid #cbd5e1;padding-top:16px}}
a{{color:#1d4ed8}}pre{{padding:14px;background:#f1f5f9;overflow:auto}}details{{margin:12px 0}}
.scroll{{overflow-x:auto}}code{{background:#f1f5f9}}</style>
<h1>RuntimeTrendDetector: решения, пороги и причины</h1>
<p>3 ноября 2026. {len(rows)} сценариев: {legacy_count} исходных и {bond_count} рядов из тестов Bond/PredictionIntervalDetector.
Фиксированные seed, исходные значения перенесены без изменения. Параметры тренд-детектора заданы явно
для единиц и временного масштаба каждого примера; автоматического подбора по результату нет.</p>
{test_text}
<h2>Как читать отчёт</h2>
<p>В каждом сценарии четыре графика: исходный ряд и моменты тревог; робастный наклон с порогами;
остаточный второй момент с порогом; причина каждого решения. Красный треугольник — рост,
голубой — падение. Серая область — прогрев, жёлтая — заданный эпизод, фиолетовая — разобранное окно.
Под графиком раскрывается это окно с линией регрессии и исходными/ограниченными остатками.</p>
<pre>ready: samples >= min_samples и span >= min_coverage * window_seconds
step_like? → step
residual_variance > variance_threshold? → variance
abs(tangent) <= min_tangent? → flat
недостаточно последовательных подтверждений? → confirmation
иначе → trend: up / down</pre>
<p>Тангенс измеряется в (value/value_scale) за минуту; это не угол и не процент. Остаточный момент —
средний квадрат ограниченных остатков после удаления робастной линии, а не дисперсия всего ряда.
Более высокий порог дисперсии разрешает более шумные окна. Причина показывает первый блокирующий
фильтр в порядке выше; CSV дополнительно содержит независимые проверки наклона и дисперсии.</p>
<p>Сэмплы обновляются с исходным шагом (здесь 15 с), evaluate() выполняется раз в 60 с.
Последняя точка оценивается отдельно, если не совпала с сеткой; она помечена final.
CSV всех точек оставляет поля решения пустыми между фактическими оценками. Будущие данные
не используются. Линия выбранного окна описывает уже наблюдённое окно и не прогнозирует следующую точку.</p>
<h2>Сводка</h2><p>Готово/всего включает прогрев. В колонке «вне трендового контекста» учитывается
пересечение всего окна с размеченным монотонным трендом, поэтому память о закончившемся тренде
не объявляется ложной тревогой сразу после его конца. Для циклов и реконструкции без эталона стоит «—».
Это число оценок, не число независимых инцидентов. Контроль чувствительности намеренно демонстрирует
ложные тревоги; выполненный критерий там означает воспроизведение этого недостатка.</p>
<div class="scroll"><table><tr><th>Ряд</th><th>Окно, мин</th><th>Готово/всего</th><th>Рост/падение</th><th>Тревог среди готовых</th>
<th>Первая тревога, мин</th><th>Вне трендового контекста</th><th>Критерий</th></tr>{''.join(table)}</table></div>
<p><a href="summary.json">Полная сводка JSON</a> · <a href="summary.csv">Сводка CSV</a> ·
<a href="reasons.csv">Частоты причин CSV</a></p><img src="overview.png" alt="Реакции и причины по всем сценариям">
<h2>Ограничения интерпретации</h2>
<p>Пороги и проверка ступеньки — инженерные правила, не p-value и не гарантия частоты ложных тревог.
Локальный участок цикла или шум с памятью может выглядеть как тренд; ступенька может пройти фильтр
на краю окна. На новых примерах показаны все наблюдаемые реакции, без требования повторить балл Bond:
тот детектор оценивает неожиданность отдельного значения, а этот — наклон окна.
Задержка считается от начала размеченного тренда и включает прогрев, если он ещё не завершён.
Для рядов без разметки не вычисляются выдуманные precision/recall или задержки.</p>
{''.join(sections)}
<h2>Воспроизведение</h2><pre>uv run python -m unittest discover -s tests -t . -v *> runtime-trend-test-results.txt
uv run python -m apps.demo --output artifacts/runtime-trend --test-log runtime-trend-test-results.txt</pre>
<p>Python {report['python']}; NumPy {report['numpy']}; Matplotlib {report['matplotlib']}.
SHA256 исходника детектора: <code>{report['detector_source_sha256']}</code>.</p></html>'''
    (output/'report.html').write_text(page, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/runtime-trend'))
    parser.add_argument('--test-log', type=Path)
    args = parser.parse_args()
    report = create_report(args.output, test_log=args.test_log)
    print(json.dumps({'report': str((args.output/'report.html').resolve()),
                      'scenarios': report['scenario_count'], 'test_run': report['test_run'],
                      'observations': {r['slug']: r['alarm_evaluations'] for r in report['scenarios']}},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
