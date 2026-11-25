"""Reproducible interval report: previous report fixtures, CSV, PNG, HTML, JSON."""

import argparse
import csv
import html
import json
from pathlib import Path
import platform
import re

import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
import numpy as np
import scipy

from apps.detectors import PredictionIntervalDetector
from tests.prediction_interval_scenarios import (
    IntervalScenario, previous_report_scenarios, stability_scenarios,
)


def coverage(results):
    scored = [r for r in results if r.ready]
    return float(np.mean([r.score == 0 for r in scored])) if scored else None


def independent_normal_trials():
    rows = []
    for window in (5, 30):
        rng = np.random.default_rng(43 + window)
        detector = PredictionIntervalDetector(window)
        results = [detector.fit(row[:-1], calibrate=False).update(row[-1])
                   for row in rng.normal(size=(5000, window+1))]
        rows.append({'window': window, 'independent_trials': len(results),
                     'nominal_coverage': 0.95, 'coverage': coverage(results)})
    return rows


def calibration_experiment():
    # Use a separate future segment, never the calibration residuals as a test set.
    rows = []
    for mode in ('manual', 'fitted', 'automatic'):
        detector = PredictionIntervalDetector(20, scale=0.2, auto_scale=mode == 'automatic')
        detector.fit(range(300), calibrate=mode == 'fitted')
        results = [detector.update(v) for v in range(300, 1900)]
        rows.append({'mode': mode, 'training_points': 300, 'test_points': len(results),
                     'fitted_scale': detector.fitted_scale, 'final_scale': detector.scale,
                     'coverage_all_test': coverage(results),
                     'coverage_after_400_test_points': coverage(results[400:]),
                     'max_score': max(r.score for r in results)})
    return rows


def report_scenario(output, scenario):
    n = scenario.window
    plain = PredictionIntervalDetector(n, scale=scenario.scale)
    adaptive = PredictionIntervalDetector(n, scale=scenario.scale, auto_scale=True)
    results = plain.predict_intervals(scenario.values)
    auto = adaptive.predict_intervals(scenario.values)
    x = np.arange(len(results))
    lower = np.array([r.infimum if r.ready else np.nan for r in results])
    upper = np.array([r.supremum if r.ready else np.nan for r in results])
    auto_lower = np.array([r.infimum if r.ready else np.nan for r in auto])
    auto_upper = np.array([r.supremum if r.ready else np.nan for r in auto])
    outside = np.array([r.ready and r.score > 0 for r in results])
    muted = outside & np.array([r.score_factor < 1 for r in results])
    strong = outside & ~muted
    with (output / f'{scenario.name}.csv').open('w', encoding='utf-8', newline='') as target:
        writer = csv.writer(target)
        writer.writerow(('index', 'value', 'ready', 'infimum', 'supremum', 'score',
                         'raw_score', 'interval_change', 'score_factor', 'scale',
                         'auto_infimum', 'auto_supremum', 'auto_score', 'auto_raw_score',
                         'auto_interval_change', 'auto_score_factor', 'auto_scale'))
        writer.writerows((i, value, a.ready, a.infimum, a.supremum, a.score,
                          a.raw_score, a.interval_change, a.score_factor, a.scale,
                          b.infimum, b.supremum, b.score, b.raw_score,
                          b.interval_change, b.score_factor, b.scale)
                         for i, (value, a, b) in enumerate(zip(scenario.values, results, auto)))
    fig, axes = plt.subplots(4, 1, figsize=(13, 9), sharex=True,
                             gridspec_kw={'height_ratios': (3, 1.4, 1, 1)}, layout='constrained')
    fig.suptitle(f'{scenario.label}\nN={n}; confidence=95%; scale={scenario.scale}; source: {scenario.source}', fontsize=12)
    axes[0].fill_between(x, lower, upper, alpha=0.2, color='#2563eb',
                         label=f'Интервал, scale={scenario.scale}')
    axes[0].plot(x, scenario.values, color='#334155', linewidth=0.85, label='Наблюдение')
    axes[0].plot(x, auto_lower, color='#ea580c', linewidth=0.65, linestyle='--', label='Автомасштаб')
    axes[0].plot(x, auto_upper, color='#ea580c', linewidth=0.65, linestyle='--')
    axes[0].scatter(x[muted], scenario.values[muted], color='#64748b', s=5, alpha=0.5, zorder=3,
                    label='Промах: балл ×0.1')
    axes[0].scatter(x[strong], scenario.values[strong], color='#dc2626', s=12, zorder=3,
                    label='Промах: окно меняется')
    axes[0].set_ylabel('Значение')
    axes[0].legend(fontsize=8, loc='best', ncol=2)
    axes[1].plot(x, [r.raw_score for r in results], color='#94a3b8', linewidth=0.9,
                 label='До ослабления: raw_score')
    axes[1].plot(x, [r.score for r in results], color='#2563eb', label='Итоговый score (fixed)')
    axes[1].plot(x, [r.score for r in auto], color='#ea580c', alpha=0.8, label='Автомасштаб')
    axes[1].set_ylabel('Балл [0, 1)')
    axes[1].set_ylim(-0.03, 1.03)
    axes[1].legend(fontsize=8, ncol=3, loc='upper right')
    axes[2].plot(x, [r.interval_change for r in results], color='#7c3aed', linewidth=0.8)
    axes[2].axhline(plain.stability_threshold, color='#64748b', linewidth=0.8, linestyle='--')
    axes[2].set_ylabel('Сдвиг границ')
    axes[2].set_ylim(-0.03, 1.03)
    axes[3].plot(x, [r.scale for r in auto], color='#ea580c')
    axes[3].axhline(scenario.scale, color='#2563eb', linewidth=0.7, linestyle=':')
    axes[3].set_ylabel('Коэффициент')
    axes[3].set_xlabel('Индекс точки (интервал построен до её наблюдения)')
    for ax in axes:
        ax.axvspan(0, n-1, color='#64748b', alpha=0.12)
        if scenario.start is not None:
            ax.axvspan(scenario.start, scenario.end-1, color='#f59e0b', alpha=0.09)
        ax.grid(alpha=0.18)
    fig.savefig(output / f'{scenario.name}.png', dpi=115)
    plt.close(fig)
    return {'name': scenario.name, 'label': scenario.label, 'source': scenario.source,
            'window': n, 'initial_scale': scenario.scale,
            'points': len(results), 'scored_points': len(results)-n,
            'event_start': scenario.start, 'event_end_exclusive': scenario.end,
            'fixed_coverage': coverage(results), 'auto_coverage': coverage(auto),
            'max_score': max(r.score for r in results),
            'max_raw_score': max(r.raw_score for r in results),
            'misses': int(np.count_nonzero(outside)),
            'attenuated_misses': int(np.count_nonzero(muted)),
            'max_auto_score': max(r.score for r in auto),
            'last_auto_scale_used': auto[-1].scale,
            'max_auto_scale': max(r.scale for r in auto),
            'event_first_score': results[scenario.start].score if scenario.start is not None else None}


def stability_comparison(output, scenarios):
    """Actual before/after scores on the screenshot fixture and new controls."""
    comparisons = []
    for scenario in scenarios:
        if scenario.name != 'acf-periodic_new' and not scenario.name.startswith('stability-'):
            continue
        with (output / f'{scenario.name}.csv').open(encoding='utf-8', newline='') as source:
            rows = list(csv.DictReader(source))
        phases = [('stationary', scenario.window, len(rows))]
        if scenario.start is not None:
            phases = [('before', scenario.window, scenario.start),
                      ('after', scenario.end+scenario.window, len(rows))]
        for mode, prefix in (('fixed', ''), ('auto', 'auto_')):
            for phase, start, end in phases:
                section = rows[start:end]
                raw = np.array([float(r[prefix+'raw_score']) for r in section])
                weighted = np.array([float(r[prefix+'score']) for r in section])
                factors = np.array([float(r[prefix+'score_factor']) for r in section])
                mass_ratio = float(weighted.sum()/raw.sum()) if raw.sum() else None
                comparisons.append({'name': scenario.name, 'mode': mode, 'phase': phase,
                                    'start': start, 'end_exclusive': end, 'points': len(section),
                                    'misses': int(np.count_nonzero(raw)),
                                    'attenuated_misses': int(np.count_nonzero((raw > 0) & (factors < 1))),
                                    'score_mass_ratio': mass_ratio,
                                    'raw_above_0_1': int(np.count_nonzero(raw > 0.1)),
                                    'score_above_0_1': int(np.count_nonzero(weighted > 0.1))})
    return comparisons


def create_report(output, test_log=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    scenarios = list(previous_report_scenarios())
    scenarios.append(IntervalScenario('underwide-trend', 'Узкий интервал: контроль масштабирования',
                                      np.arange(1600, dtype=float), 'new control', window=20, scale=0.2))
    for seed in (11, 43, 55):
        scenarios.extend(stability_scenarios(seed))
    rows = []
    for scenario in scenarios:
        rows.append(report_scenario(output, scenario))
    trials = independent_normal_trials()
    calibration = calibration_experiment()
    stability = stability_comparison(output, scenarios)
    test_run = None
    if test_log is not None:
        log = Path(test_log).read_text(encoding='utf-8')
        (output / 'test-results.txt').write_text(log, encoding='utf-8')
        match = re.search(r'Ran (\d+) tests in ([\d.]+)s', log)
        if match is None or not re.search(r'^OK(?: \(skipped=\d+\))?$', log, re.MULTILINE):
            raise ValueError('test log must contain a successful complete unittest run')
        skipped = re.search(r'^OK \(skipped=(\d+)\)', log, re.MULTILINE)
        test_run = {'total': int(match[1]), 'seconds': float(match[2]),
                    'skipped': int(skipped[1]) if skipped else 0, 'status': 'OK'}
    summary = {'confidence': 0.95, 'manual_scale': 1, 'adaptation_rate': 0.05,
               'max_scale': 100, 'noise_floor': 1e-9,
               'stability_threshold': 0.3, 'stable_score_factor': 0.1,
               'python': platform.python_version(), 'numpy': np.__version__, 'scipy': scipy.__version__,
               'scenario_count': len(rows), 'scenarios': rows, 'test_run': test_run,
               'independent_normal_trials': trials, 'held_out_calibration': calibration,
               'stability_comparison': stability}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    with (output / 'summary.csv').open('w', encoding='utf-8', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    table = ''.join(f'<tr><td><a href="#{r["name"]}">{html.escape(r["label"])}</a></td>'
                    f'<td>{r["window"]}</td><td>{r["scored_points"]}</td>'
                    f'<td>{r["fixed_coverage"]:.2%}</td><td>{r["auto_coverage"]:.2%}</td>'
                    f'<td>{r["max_score"]:.4f}</td><td>{r["max_auto_scale"]:.3f}</td></tr>' for r in rows)
    normal_table = ''.join(f'<tr><td>{r["window"]}</td><td>{r["independent_trials"]}</td>'
                           f'<td>{r["coverage"]:.2%}</td></tr>' for r in trials)
    calibration_table = ''.join(f'<tr><td>{r["mode"]}</td><td>{r["fitted_scale"]:.4f}</td>'
                                f'<td>{r["final_scale"]:.4f}</td><td>{r["coverage_all_test"]:.2%}</td>'
                                f'<td>{r["coverage_after_400_test_points"]:.2%}</td></tr>' for r in calibration)
    stability_table = ''.join(
        f'<tr><td>{r["name"]}</td><td>{r["mode"]}</td><td>{r["phase"]}</td>'
        f'<td>{r["attenuated_misses"]}/{r["misses"]}</td><td>{r["score_mass_ratio"]:.3f}</td>'
        f'<td>{r["raw_above_0_1"]} → {r["score_above_0_1"]}</td></tr>' for r in stability)
    figures = ''.join(f'<article id="{r["name"]}"><h2>{html.escape(r["label"])}</h2>'
                      f'<p>Источник: {html.escape(r["source"])}. N={r["window"]}. '
                      f'<a href="{r["name"]}.csv">Все точки и границы CSV</a></p>'
                      f'<img src="{r["name"]}.png" loading="lazy" alt="{html.escape(r["label"], quote=True)}">'
                      '</article>' for r in rows)
    test_text = (f'<p><strong>Полный прогон: {test_run["total"]} тестов; '
                 f'{test_run["total"]-test_run["skipped"]} успешных; '
                 f'{test_run["skipped"]} пропущен (опциональный live VictoriaMetrics).</strong> '
                 '<a href="test-results.txt">Полный журнал</a></p>' if test_run else '')
    page = f'''<!doctype html><html lang="ru"><meta charset="utf-8">
<title>PredictionIntervalDetector — проверка интервалов</title>
<style>body{{font:16px/1.55 system-ui;margin:30px auto;max-width:1300px;padding:0 20px;color:#1e293b}}
table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:7px;border-bottom:1px solid #ddd;text-align:left}}
thead{{background:#f1f5f9}}img{{width:100%;height:auto}}article{{margin:48px 0}}a{{color:#1d4ed8}}
code,pre{{background:#f1f5f9;padding:3px}}pre{{padding:15px;overflow:auto}}</style>
<h1>PredictionIntervalDetector: интервал для каждой следующей точки</h1>
<p>Дата: 2 ноября 2026. {len(rows)} сценариев. Исходные генераторы и фикстуры взяты из прежних
отчётов ACF, CUSUM, runtime-trend, correlation, generator и тестов MAD.
Реконструкция со скриншота показана при шагах 1 минута и 15 секунд; это приближённые данные,
а не исходная телеметрия. У всех точек в CSV есть отдельная строка, включая прогрев и последнюю точку.</p>
<h2>Что именно проверяется</h2>
{test_text}
<pre>mu = mean(x[t-N:t]); s = sample_std(x[t-N:t], ddof=1)
h0 = max(noise_floor, ulp(mu), t_(1-alpha/2, N-1) * s * sqrt(1+1/N))
[infimum, supremum] = [mu-scale*h0, mu+scale*h0]
raw_score = 0 внутри границ (включая равенство)
raw_score = d/(scale*h0+d) снаружи, d = расстояние до ближайшей границы
change = min(1, max(abs(inf_next-inf), abs(sup_next-sup)) / max(h, h_next))
factor = 0.1, если change &lt;= 0.3, иначе 1
score = raw_score * factor
scale_next = clip(scale * exp(rate * (miss-alpha)), initial_scale, max_scale)</pre>
<p>Точка t не участвует в построении своих прогнозных границ. Изменение границ сравнивает окно
до и после добавления t; обе пары границ вычисляются при одном текущем масштабе, h и h_next —
полуширины. Это квалификация тяжести по уже наблюдаемой точке, без будущих данных.
Точка поступает в историю после оценки. Авторегуляция масштаба действует со следующей точки
и считает фактические промахи независимо от ослабления балла.
Ограничение nextafter(1,0) сохраняет score &lt; 1 даже при округлении очень больших отклонений.
Это предиктивный интервал отдельного будущего наблюдения, не интервал среднего.</p>
<p>Номинальные 95% относятся к IID нормальным данным при scale=1 и без активного ограничения
минимальной ширины. На зависимых рядах, трендах, циклах и тяжёлых хвостах покрытие измеряется
эмпирически. Частота промахов не равна вероятности аномалии, а изменение периодичности может
оставаться внутри интервала. Длительное плато становится новым фоном через N наблюдений.</p>
<h2>Ослабление фоновых промахов при стабильном интервале</h2>
<p>По умолчанию изменение не больше 30% большей полуширины считается небольшим:
<code>stability_threshold=0.3</code>, <code>stable_score_factor=0.1</code>.
Значение factor=1 отключает ослабление. Границы, покрытие и обучение масштаба не меняются.
Количество ненулевых оценок сохраняется; уменьшается их величина. Условный порог 0.1 ниже
приведён только для сравнения и не является встроенным правилом тревоги.</p>
<p>В строках before/after показан сценарий из присланного изображения: появление цикла,
слабый шум до эпизода и после заполнения окна восстановившимся фоном.
Дополнительно проверены высокий средний уровень, слабый цикл и MA(8) на трёх seed.
Отношение суммы итоговых баллов к исходным 0.1 означает ослабление ровно в 10 раз.</p>
<table><tr><th>Ряд</th><th>Режим</th><th>Участок</th><th>Ослаблено/промахов</th>
<th>Сумма score/raw</th><th>Баллов &gt; 0.1: до → после</th></tr>{stability_table}</table>
<h2>Независимые проверки покрытия</h2>
<p>В каждой попытке новое независимое окно и одна будущая точка, фиксированные seed.
При 5000 попытках стандартная ошибка покрытия около 0.31 процентного пункта.</p>
<table><tr><th>N</th><th>Попыток</th><th>Покрытие (цель 95%)</th></tr>{normal_table}</table>
<h2>Обучение и авторегуляция на отдельном будущем участке</h2>
<p>Контрольный линейный ряд, N=20, исходный scale=0.2. Обучение: точки 0…299;
проверка: 300…1899. fit использует квантиль нормированных ошибок прогнозов по предыдущим
окнам. Для ручной и автоматической конфигураций обучение калибровку не выполняет.
Покрытие на этом искусственном тренде не является гарантией для других рядов.</p>
<table><tr><th>Режим</th><th>После fit</th><th>Конечный scale</th><th>Весь test</th><th>После 400 test-точек</th></tr>
{calibration_table}</table>
<h2>Сценарии из прежних отчётов</h2>
<p>По умолчанию N=30. Для коротких response-time примеров N=8 (2 минуты при шаге 15 с).
Серая область — прогрев, жёлтая — заданный эпизод, если он выделен в исходном fixture.
Голубая область — прогноз при фиксированном коэффициенте; пунктир — авторегулируемый.
Серым отмечены ослабленные промахи, красным — промахи при заметном изменении окна.
На втором графике серый raw_score показывает прежнюю реакцию, синий — итоговую,
оранжевый — итоговую при авторегуляции. На третьем графике — движение границ и порог ослабления.
Покрытие считается только по готовым прогнозам.</p>
<table><thead><tr><th>Сценарий</th><th>N</th><th>Прогнозов</th><th>Покрытие fixed</th>
<th>Покрытие auto</th><th>Max score fixed</th><th>Max scale auto</th></tr></thead><tbody>{table}</tbody></table>
<p><a href="summary.json">JSON</a> · <a href="summary.csv">Сводка CSV</a></p>
{figures}
<h2>Воспроизведение</h2><pre>uv run python -m unittest discover -s tests -t . -v
uv run python -m tests.visualize_prediction_interval --output artifacts/prediction-interval</pre>
<p>Формула отдельного предиктивного интервала: <a href="https://www.itl.nist.gov/div898/software/dataplot/refman1/auxillar/predlimi.htm">NIST</a>.
Квантили: <a href="https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.t.html">SciPy t</a>.
Нормировка score, эмпирическая калибровка и авторегуляция — определения этой реализации.</p>
</html>'''
    (output / 'report.html').write_text(page, encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/prediction-interval'))
    parser.add_argument('--test-log', type=Path, help='Optional successful unittest log to bundle')
    args = parser.parse_args()
    summary = create_report(args.output, args.test_log)
    print(json.dumps({'scenario_count': summary['scenario_count'],
                      'normal_trials': summary['independent_normal_trials'],
                      'calibration': summary['held_out_calibration']}, indent=2))


if __name__ == '__main__':
    main()
