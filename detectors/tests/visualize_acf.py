"""Reproducible synthetic report: python -m tests.visualize_acf --output artifacts/acf."""

import argparse
import csv
from dataclasses import asdict
import html
import json
from pathlib import Path
import platform
import re
import shutil
import time

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from apps.detectors import ACFDetector
from tests.acf_scenarios import persistence_scenarios
from tests.visualize_acf_replays import create_appendix, render_appendix
from tests.acf_rps_scenario import FIXTURES as RPS_FIXTURES, STEM as RPS_STEM
from tests.visualize_acf_rps import create_rps_appendix, render_rps_appendix
from tests.acf_service_scenarios import FIXTURES as SERVICE_FIXTURES, STEM as SERVICE_STEM
from tests.visualize_acf_services import create_services_appendix, render_services_appendix


def replay(detector, values):
    scores, depths, ready = [], [], []
    peak = None
    for value in values:
        scores.append(detector.update(value))
        result = detector.result
        depths.append(result.depth)
        ready.append(result.ready)
        if result.ready and (peak is None or result.score > peak.score):
            peak = result
    return np.asarray(scores), np.asarray(depths), ready, peak


def save_csv(path, headers, rows):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)


def scenario_report(output, name, label, reference, values, start, end, **settings):
    detector = ACFDetector(**settings).fit(reference)
    scores, depths, ready, peak = replay(detector, values)
    x = np.arange(len(values)) * 0.25
    fig, axes = plt.subplots(4, 1, figsize=(12, 10), constrained_layout=True)
    fig.suptitle(label, fontsize=16)
    axes[0].plot(x, values, color='#275d89', lw=1)
    axes[0].set_ylabel('Входной ряд')
    axes[1].plot(x, scores, color='#bd4c37', lw=1.5)
    axes[1].set_ylabel('Балл [0, 1]')
    axes[1].set_ylim(-0.01, max(0.1, float(scores.max()) * 1.15))
    axes[2].step(x, depths, color='#6b499c', where='post')
    axes[2].set_ylabel('Глубина, лаги')
    axes[2].set_ylim(-0.5, detector.max_lag + 1)
    for ax in axes[:3]:
        ax.axvspan(start*0.25, end*0.25, color='#e5b85c', alpha=0.18)
        ax.axvspan(0, (detector.window-1)*0.25, color='gray', alpha=0.10)
        ax.set_xlabel('Минуты при шаге 15 секунд')
        ax.grid(alpha=0.2)
    lags = np.arange(1, detector.max_lag+1)
    axes[3].plot(lags, peak.acf[1:], label='Текущий ACF при максимуме балла')
    axes[3].plot(lags, peak.reference_acf[1:], label='ACF здорового фона')
    axes[3].fill_between(lags,
                         np.asarray(peak.reference_acf[1:])-peak.thresholds[1:],
                         np.asarray(peak.reference_acf[1:])+peak.thresholds[1:],
                         color='gray', alpha=0.15, label='Эвристическая полоса')
    axes[3].set_xlabel('Лаг, отсчёты')
    axes[3].set_ylabel('ACF')
    axes[3].set_ylim(-1.1, 1.1)
    axes[3].legend(fontsize=9, loc='best')
    axes[3].grid(alpha=0.2)
    fig.savefig(output / f'{name}.png', dpi=120)
    plt.close(fig)
    save_csv(output / f'{name}.csv',
             ('index', 'seconds', 'value', 'ready', 'score', 'depth', 'depth_seconds'),
             zip(range(len(values)), x*60, values, ready, scores, depths, depths*15))
    save_csv(output / f'{name}_reference.csv', ('index', 'value'), enumerate(reference))
    save_csv(output / f'{name}_peak_lags.csv',
             ('lag', 'acf', 'reference_acf', 'change', 'threshold', 'significant'),
             ((k, peak.acf[k], peak.reference_acf[k], peak.lag_changes[k],
               peak.thresholds[k], k in peak.significant_lags)
              for k in range(detector.max_lag+1)))
    after = min(len(values)-1, end+detector.window)
    baseline = scores[detector.window-1:start]
    return {'name': name, 'label': label, 'window': detector.window,
            'max_lag': detector.max_lag, 'reference_samples': len(reference),
            'start': start, 'end': end, 'max_score': float(scores.max()),
            'max_depth': int(depths.max()), 'depth_at_peak_score': peak.depth,
            'peak_index': int(np.argmax(scores)),
            'baseline_nonzero_fraction': float(np.mean(baseline > 0)) if len(baseline) else None,
            'score_after_full_recovery_window': float(scores[after]),
            'peak': asdict(peak)}


def ideal_experiment():
    rows = []
    for duration in (1, 4, 16, 64, 256):
        values = np.r_[np.zeros(256-duration), np.ones(duration)]
        result = ACFDetector().fit(np.zeros(512)).analyze(values)
        rows.append({'duration': duration, 'score': result.score,
                     'depth': result.depth, 'horizon_reached': result.horizon_reached})
    return rows


def noise_experiment():
    # Independent local windows within each seed; common fitted reference per seed.
    # This is an empirical fixture, not a guarantee or a formal significance level.
    rates, max_score = [], 0.0
    for seed in range(40):
        rng = np.random.default_rng(seed+1000)
        detector = ACFDetector().fit(rng.normal(size=512))
        results = [detector.analyze(rng.normal(size=256)) for _ in range(25)]
        rates.extend(result.score > 0 for result in results)
        max_score = max(max_score, max(result.score for result in results))
    return {'seeds': 40, 'windows_per_seed': 25, 'windows': len(rates),
            'nonzero_windows': int(sum(rates)), 'nonzero_fraction': float(np.mean(rates)),
            'max_score': max_score}


def benchmark():
    rng = np.random.default_rng(43)
    detector = ACFDetector().fit(rng.normal(size=512))
    for value in rng.normal(size=256):
        detector.update(value)
    timings = []
    for value in rng.normal(size=1000):
        before = time.perf_counter()
        detector.update(value)
        timings.append((time.perf_counter()-before)*1000)
    return {'updates': len(timings), 'median_ms': float(np.median(timings)),
            'p95_ms': float(np.quantile(timings, 0.95)),
            'mean_ms': float(np.mean(timings)), 'platform': platform.platform(),
            'python': platform.python_version()}


def create_report(output, test_log=None):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10})
    rows = [scenario_report(output, *scenario) for scenario in persistence_scenarios()]
    # User's concrete case: 40 observations at 15 s = 10 minutes. The shifted
    # level stays forever, but ACF drops when its transition exits the window.
    rows.append(scenario_report(output, 'step_40', 'Новый уровень становится нормой: окно 10 минут',
                                np.zeros(160), np.r_[np.zeros(100), np.ones(160)*7],
                                100, 260, window=40, max_lag=10, reference_window=160))
    ideal, noise, timing = ideal_experiment(), noise_experiment(), benchmark()
    summary = {'sample_interval_seconds': 15, 'scenarios': rows,
               'ideal_plateaus': ideal, 'white_noise': noise, 'benchmark': timing}
    summary['replay_appendix'] = create_appendix(output)
    if (RPS_FIXTURES/(RPS_STEM+'.json')).is_file():
        summary['rps_appendix'] = create_rps_appendix(output)
    if (SERVICE_FIXTURES/(SERVICE_STEM+'.json')).is_file():
        summary['services_appendix'] = create_services_appendix(output)
    attach_test_log(summary, output, test_log)
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                         encoding='utf-8')
    save_csv(output / 'summary.csv', ('name', 'window', 'max_score', 'depth_at_peak_score', 'max_depth',
                                     'score_after_full_recovery_window'),
             ((r['name'], r['window'], r['max_score'], r['depth_at_peak_score'], r['max_depth'],
               r['score_after_full_recovery_window']) for r in rows))
    render_html(output, summary)
    return summary


def render_html(output, summary):
    rows = summary['scenarios']
    ideal, noise, timing = summary['ideal_plateaus'], summary['white_noise'], summary['benchmark']
    replay_nav = ('<p><a href="#replay-inputs">Сценарий из InfluxDB, 17–18 октября, и Cassandra</a></p>'
                  if summary.get('replay_appendix') else '')
    check = summary.get('replay_test_run')
    if check:
        replay_nav += (f'<p>Проверки после исправления графика: {check["tests"]} тестов, пропущено {check["skipped"]}. '
                       '<a href="replay-test-results.txt">Журнал</a>.</p>')
    if summary.get('rps_appendix'):
        replay_nav += '<p><a href="#rps-influx">Добавлено 9 ноября: RPS из InfluxDB по последнему графику</a></p>'
    if summary.get('services_appendix'):
        replay_nav += '<p><a href="#service-response-acf">Добавлено: ACF четырех сервисов за 5 октября</a></p>'
    ideal_table = ''.join(f"<tr><td>{r['duration']}</td><td>{r['score']:.5f}</td>"
                          f"<td>{r['depth']}</td><td>{r['horizon_reached']}</td></tr>" for r in ideal)
    table = ''.join(f"<tr><td><a href='#{r['name']}'>{html.escape(r['label'])}</a></td>"
                    f"<td>{r['max_score']:.5f}</td><td>{r['depth_at_peak_score']}</td><td>{r['max_depth']}</td>"
                    f"<td>{r['score_after_full_recovery_window']:.5f}</td></tr>" for r in rows)
    cycle_rows = [r for r in rows if r['name'].startswith('cycle_')]
    cycle_table = ''.join(
        f"<tr><td><a href='#{r['name']}'>{html.escape(r['label'])}</a></td>"
        f"<td>{r['max_score']:.5f}</td><td>{r['depth_at_peak_score']}</td>"
        f"<td>{r['score_after_full_recovery_window']:.5f}</td></tr>" for r in cycle_rows)
    figures = ''.join(f"<section id='{r['name']}'><h2>{html.escape(r['label'])}</h2>"
                      f"<img src='{r['name']}.png' alt='{html.escape(r['label'])}'>"
                      f"<p><a href='{r['name']}.csv'>Ряд и результаты CSV</a> · "
                      f"<a href='{r['name']}_peak_lags.csv'>Профиль по лагам CSV</a></p></section>"
                      for r in rows)
    document = f'''<!doctype html><html lang="ru"><meta charset="utf-8">
<title>ACFDetector — отчет о проверке</title><style>
body{{font:16px/1.55 system-ui,sans-serif;max-width:1160px;margin:36px auto;padding:0 24px;color:#182a3a}}
h1,h2{{line-height:1.2}}table{{border-collapse:collapse;width:100%;margin:20px 0}}
td,th{{border:1px solid #d2dce4;padding:8px;text-align:left}}th{{background:#e9f0f5}}
img{{width:100%;height:auto}}code,pre{{background:#edf1f4}}pre{{padding:16px;overflow:auto}}
section{{border-top:1px solid #cad6df;margin-top:40px;padding-top:20px}}a{{color:#20598c}}
</style><h1>ACFDetector: изменение памяти временного ряда</h1>
<p>Дата: 28 октября 2026. Воспроизводимые синтетические данные, фиксированные seed.
Результаты оценивают временную зависимость, а не причинность, тип или величину аномалии.</p>
<p><a href="#cycle_changes">Изменение цикличности по примеру</a></p>
{replay_nav}
<p><b>Принятая семантика:</b> из каждого окна вычитается его собственное среднее.
Если новый уровень заполнил окно, переход ушел в прошлое и стал новой нормой.
Для окна 40 точек при шаге 15 секунд это 10 минут. Прежнее среднее не удерживается.</p>
<p>ACF текущего окна сравнивается со здоровым фиксированным ACF.
Обычная память и обычные циклы не должны автоматически считаться новым воздействием.
Чистая смена уровня забывается; новая структура корреляций отличается от здорового фона,
пока оператор не обновит его через fit().</p>
<pre>rho[k] = sum((x[t]-mean(x))*(x[t+k]-mean(x))) / sum((x[t]-mean(x))**2)
delta[k] = abs(rho_current[k] - rho_healthy[k])
band[k] = max(min_change, threshold * sqrt((1+2*sum(rho_healthy[j]**2, j=1..k-1))*(1/N+1/M)))
excess[k] = min(1, max(0, delta[k]-band[k]))
depth = последний лаг, где delta[k] &gt; band[k]; 0, если таких нет
score = sum(excess[k], k=1..max_lag) / max_lag</pre>
<p>Полоса ограничивается сверху 2. Порог эвристический, по мотивам формулы Бартлетта;
это не p-value и не гарантия частоты ложных тревог. Лаг 0 исключен из оценки.
При нулевой энергии используется нулевой профиль с отдельным признаком zero_energy.</p>
<p>По умолчанию N=256, M=512, max_lag=32, threshold=3.5, min_change=0.1.
В базовом сценарии step_40: N=40, M=160, max_lag=10. Порог бинарного алерта не назначается.</p>
<h2>Контроль длительности на идеальном прямоугольном воздействии</h2>
<p>Снимок окна N=256: сначала нули, затем d единиц; здоровый фон нулевой.
Полное плато d=256 стало нормой и закономерно дает 0.</p>
<table><tr><th>d, точек</th><th>score</th><th>depth, лагов</th><th>Достигнут горизонт 32</th></tr>{ideal_table}</table>
<h2>Результаты сценариев</h2><p>Максимумы взяты по всему прогону; последняя колонка —
оценка после полного окна восстановления. Показаны отдельно глубина при максимальном
балле и максимальная глубина за весь прогон. Глубина — дальность статистической связи,
не точная длительность воздействия. Желтая область — введенное воздействие;
серая область слева — прогрев.</p>
<table><tr><th>Сценарий</th><th>Макс. score</th><th>Depth при макс. score</th><th>Макс. depth</th><th>После восстановления</th></tr>{table}</table>
<p>В контрольном сценарии смены только разброса возник слабый балл около 0.013.
При коротком восстановлении tau=2 отдельные слабые отклонения дали depth=20,
тогда как при максимальном балле depth=2. Поэтому depth следует читать совместно
со score и significant_lags: один дальний лаг сам по себе не означает длительное сильное воздействие.
Порог алерта должен проверяться на реальных результатах предшествующих детекторов.</p>
<h2 id="cycle_changes">Изменение цикличности по присланному примеру</h2>
<p>Добавлены пять синтетических сценариев: увеличение и уменьшение периода,
нарушение регулярности, постепенное изменение периода с ростом амплитуды,
а также контроль с ростом только амплитуды. Смешанный сценарий воспроизводит
характер присланного графика: обычные циклы постепенно меняют форму временной связи.
Все периоды указаны в отсчетах; исходные численные значения на изображении не заданы.</p>
<p>В начале период 16 точек. Воздействие действует на участке [450, 1050),
после чего восстанавливается прежний период. Фаза непрерывна на границах;
здоровый фон содержит тот же цикл и независимый шум. В контрольном сценарии
амплитуда всего сигнала вместе с шумом увеличивается в 3.5 раза, сохраняя отношение сигнал/шум.</p>
<table><tr><th>Сценарий</th><th>Макс. score</th><th>Depth при макс. score</th><th>После восстановления</th></tr>{cycle_table}</table>
<p>Проверка на трех seed (11, 43, 55) подтверждает реакцию на изменения периода
и регулярности, отсутствие сигнала до воздействия и после полного окна восстановления.
Рост только амплитуды при сохраненной структуре цикла дает нулевой score на этих fixtures.
При изменении периода отличие ACF от фиксированного фона сохраняется и на полностью
обновленном окне — это изменение структуры памяти ряда.</p>
<h2>Шум и производительность</h2><p>Белый шум: {noise['nonzero_windows']} из {noise['windows']}
окон имеют score&gt;0 ({noise['nonzero_fraction']:.2%}); 40 независимых seed фона,
по 25 независимых локальных окон на фон. Максимальный score={noise['max_score']:.6f}.</p>
<p>1000 последовательных update: медиана {timing['median_ms']:.3f} мс,
p95 {timing['p95_ms']:.3f} мс. Это замер одного процесса на этой машине,
не нагрузочный тест системы. Стоимость обновления O(N*max_lag), память O(M+N+max_lag).
Python {timing['python']}.</p>
<h2>Интерфейс и проверка</h2><pre>from apps.detectors import ACFDetector
d = ACFDetector(window=40, max_lag=10, reference_window=160).fit(healthy_values)
for value in detector_scores:
    score = d.update(value)
    if d.ready:
        result = d.result
        print(score, result.depth, result.significant_lags)

python -m unittest discover -s tests -t . -v
python -m tests.visualize_acf --output artifacts/acf</pre>
<p>После fit() нужен полный локальный прогрев. Без fit(): сначала M здоровых точек,
затем N других точек; reference и окно не пересекаются. reset() сохраняет явный fit().
Пакетные analyze/detect/detect_single_seria не меняют активный поток.</p>
<h2>Границы интерпретации</h2><p>Ряд должен иметь равномерный шаг; пропуски нужно обрабатывать до детектора.
Глубина измеряется в отсчетах, для перевода в секунды умножается на шаг.
horizon_reached означает, что изменение достигло последнего проверенного лага;
про более далекие лаги выводов нет. Короткие окна и большой порог снижают чувствительность.
Сглаживание предыдущими детекторами само создает память и должно входить в здоровый фон.
Масштаб и знак всего ряда не меняют ACF, но отношение сигнал/шум влияет на различимость события.
Тренды, периодичность и длинное восстановление могут давать схожие профили.</p>
<p>Обычный ACF и равномерность отсчетов: <a href="https://www.itl.nist.gov/div898/handbook/eda/section3/eda35c.htm">NIST</a>.
Нормировка и приближенные ошибки: <a href="https://www.statsmodels.org/stable/generated/statsmodels.tsa.stattools.acf.html">statsmodels ACF</a>.
Сравнение двух профилей, score и depth — инженерные определения этой реализации.</p>
<p><a href="summary.json">Полные результаты JSON</a> · <a href="summary.csv">Сводка CSV</a></p>{figures}{render_appendix(summary.get('replay_appendix'))}{render_rps_appendix(summary.get('rps_appendix'))}{render_services_appendix(summary.get('services_appendix'))}</html>'''
    (output / 'report.html').write_text(document, encoding='utf-8')


def attach_test_log(summary, output, path):
    if path is None:
        return
    contents = path.read_text(encoding='utf-8')
    match = re.search(r'Ran (\d+) tests? in ([\d.]+)s', contents)
    if not match or not re.search(r'^OK(?: \(skipped=\d+\))?\s*$', contents, re.M) or 'FAILED (' in contents:
        raise ValueError('Expected a completed successful unittest log')
    skipped = re.search(r'OK \(skipped=(\d+)\)', contents)
    summary['replay_test_run'] = {'tests': int(match[1]), 'seconds': float(match[2]),
                                  'skipped': int(skipped[1]) if skipped else 0}
    if path.resolve() != (output/'replay-test-results.txt').resolve():
        shutil.copy2(path, output/'replay-test-results.txt')


def append_replays(output, test_log=None):
    summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
    summary['replay_appendix'] = create_appendix(output)
    if (RPS_FIXTURES/(RPS_STEM+'.json')).is_file():
        summary['rps_appendix'] = create_rps_appendix(output)
    attach_test_log(summary, output, test_log)
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    render_html(output, summary)
    return summary


def append_rps(output, test_log=None):
    """Refresh RPS diagnostics while preserving other report sections."""
    summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
    summary['rps_appendix'] = create_rps_appendix(output, test_log)
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    render_html(output, summary)
    return summary


def append_services(output, test_log=None):
    """Append the linked service panel while retaining all preceding report data."""
    summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
    summary['services_appendix'] = create_services_appendix(output, test_log)
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    render_html(output, summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/acf'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--append-replays', action='store_true')
    mode.add_argument('--append-rps', action='store_true')
    mode.add_argument('--append-services', action='store_true')
    parser.add_argument('--test-log', type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.append_services:
        summary = append_services(output, args.test_log)
    elif args.append_rps:
        summary = append_rps(output, args.test_log)
    else:
        summary = append_replays(output, args.test_log) if args.append_replays else create_report(output, args.test_log)
    print(json.dumps({'report': str(output/'report.html'), 'base_scenarios': len(summary['scenarios']),
                      'replay_series': summary['replay_appendix']['series_count'],
                      'replay_points': summary['replay_appendix']['input_points'],
                      'validation': summary.get('replay_test_run'),
                      'rps_points': summary.get('rps_appendix', {}).get('points'),
                      'service_series': summary.get('services_appendix', {}).get('series_count')}, ensure_ascii=False, indent=2))



if __name__ == '__main__':
    main()
