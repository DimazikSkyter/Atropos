"""Offline CUSUM report: adaptation, small/large saws, frozen telemetry and CPU."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import html
import inspect
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from apps.detectors import CusumDetector
from tests.acf_replay_scenarios import replay_scenarios
from tests.cusum_scenarios import legacy_scenarios, noisy_scenario, real_system_scenario, saw_scenario, small_dip_scenario
from tests.cusum_grafana_report import append_grafana_replay

MSK = timezone(timedelta(hours=3), 'Europe/Moscow')
FIXTURES = Path(__file__).with_name('fixtures')
VARIANTS = {
    'adaptive': {'drift_mode': 'adaptive', 'window': 10, 'inertia_window': 200},
    'adaptive-wide': {'drift_mode': 'adaptive', 'window': 40, 'inertia_window': 800},
    'previous-sigma': {'drift_mode': 'sigma', 'window': 10, 'inertia_window': 200},
}
COLORS = {'adaptive': '#dc2626', 'adaptive-wide': '#16a34a', 'previous-sigma': '#64748b'}
LABELS = {'adaptive': 'CUSUM: адаптация, 10/200', 'adaptive-wide': 'CUSUM: адаптация, 40/800',
          'previous-sigma': 'Прежний режим: drift=3σ, 10/200'}


def cases():
    for key, values, start, end in legacy_scenarios():
        yield dict(key=key, label='Исходный сценарий: ' + key, values=values, phases=[(start, end)])
    for key, label, values, start, end in [
        ('quiet-saw', 'Появление пилы на спокойном фоне: рост и привыкание', saw_scenario(), 350, 650),
        ('noisy-saw', 'Та же пила на шумном фоне', noisy_scenario(), 350, 650),
        ('isolated-small-dip', 'Одиночная малая просадка', small_dip_scenario(), 350, 355),
        ('repeated-small-dip', 'Повторяющиеся малые просадки: новый режим становится нормой', small_dip_scenario(50), 350, 600),
        ('level-step', 'Смена уровня без возврата', np.array([100.] * 500 + [200.] * 500), 500, 501),
        ('constant', 'Постоянный уровень', np.full(1000, 100.), 500, 501),
    ]:
        yield dict(key=key, label=label, values=values, phases=[(start, end)])
    values = np.tile([100., 95., 100., 100., 100.], 240)
    for first, last in ((350, 500), (750, 850)):
        values[first:last] = np.tile([100., 50., 100., 100., 100.], (last-first)//5)
    yield dict(key='small-and-large-saw', label='Малая пила — фон; большая пила — новое аномальное движение',
               values=values, phases=[(350, 500), (750, 850)])
    data, metadata = real_system_scenario()
    yield dict(key='real-system-image', label='Реконструкция исходного изображения, шаг 1 минута',
               values=data['value'], phases=[], provenance=metadata)
    for case in replay_scenarios():
        yield dict(key=case.name, label=case.label, values=case.values, phases=[(p['start'], p['end']) for p in case.phases],
                   times=case.timestamps)
    for step in (30, 15):
        source = json.loads((FIXTURES / f'cusum-cpu-20261122-{step}s.json').read_text(encoding='utf-8'))
        yield dict(key=f'cpu-{step}s', label=f'CPU из Atropos: шаг {step} с, {len(source["values"])} точек',
                   values=np.asarray(source['values']), phases=[],
                   times=[datetime.fromtimestamp(t/1e9, MSK) for t in source['times']],
                   query=source['query'], bounds_ns=source['bounds_ns'])
    source = json.loads((FIXTURES / 'cusum-cpu-20261122-3h-15s.json').read_text(encoding='utf-8'))
    yield dict(key='cpu-3h-15s', label='CPU: полный трёхчасовой расчёт, шаг 15 с, 720 точек',
               values=np.asarray(source['values']), phases=[],
               times=[datetime.fromtimestamp(t/1e9, MSK) for t in source['times']],
               query=source['query'], bounds_ns=source['bounds_ns'])


def replay(values, config):
    detector = CusumDetector(**config)
    rows = []
    for index, value in enumerate(values):
        score = detector.update(float(value))
        rows.append({'index': index, 'value': float(value), 'ready': detector.ready, 'score': score,
                     'instant_score': detector.instant_score, 'sigma': detector.noise_std,
                     'drift_absolute': detector.drift_allowance,
                     'background_level': detector.background_level})
    return rows


def render_case(output, case, variants):
    timed = 'times' in case
    x = mdates.date2num(case['times']) if timed else np.arange(len(case['values']))
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(14, 9), gridspec_kw={'height_ratios': [2, 1.6, 1.1]})
    axes[0].plot(x, case['values'], color='#2563eb', linewidth=1, label='Исходная метрика')
    axes[0].set_ylabel('Значение')
    for key, rows in variants.items():
        axes[1].plot(x, [r['score'] if r['ready'] else np.nan for r in rows], color=COLORS[key],
                     linewidth=1.4, label=LABELS[key], linestyle='--' if key == 'previous-sigma' else '-')
    rows = variants['adaptive']
    axes[2].plot(x, [r['drift_absolute'] for r in rows], color='#d97706', label='Адаптивный допуск |дельты|')
    axes[2].plot(x, [r['sigma'] for r in rows], color='#7c3aed', alpha=.8, label='Фоновая sigma дельт')
    axes[1].set_ylim(-.025, 1.03)
    axes[1].set_ylabel('Реакция [0, 1]')
    axes[2].set_ylabel('Единицы дельты')
    axes[2].set_xlabel('Время · Europe/Moscow' if timed else 'Индекс точки')
    for axis in axes:
        for first, last in case['phases']:
            if first < len(x):
                axis.axvspan(x[first], x[min(last, len(x)-1)], color='#f59e0b', alpha=.11)
        axis.grid(axis='y', color='#e2e8f0')
        axis.legend(loc='upper left', bbox_to_anchor=(0, -.19), frameon=False, ncol=3, fontsize=9)
    if timed:
        axes[2].xaxis.set_major_formatter(mdates.DateFormatter('%d.%m\n%H:%M', tz=MSK))
    fig.suptitle(case['label'], x=.08, ha='left', fontsize=14)
    fig.subplots_adjust(left=.08, right=.98, top=.92, bottom=.10, hspace=.62)
    image = 'adaptive-' + case['key'] + '.png'
    fig.savefig(output / image, dpi=130)
    plt.close(fig)
    return image


def make_report(output):
    output.mkdir(parents=True, exist_ok=True)
    sections, summary = [], []
    for case in cases():
        variants = {key: replay(case['values'], config) for key, config in VARIANTS.items()}
        item = {'scenario': case['key'], 'label': case['label'], 'points': len(case['values']),
                'input_sha256': hashlib.sha256(np.asarray(case['values'], dtype='<f8').tobytes()).hexdigest(),
                'variants': {}}
        for key, rows in variants.items():
            filename = f'adaptive-{case["key"]}-{key}.csv'
            with (output / filename).open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            scores = np.array([r['score'] for r in rows])
            n = len(scores)
            item['variants'][key] = {'config': VARIANTS[key], 'threshold': 5., 'drift_quantile': .95,
                'drift_std_fraction': .5,
                'max_score': float(scores.max()), 'ready_points': sum(r['ready'] for r in rows),
                'samples_ge_05': int(np.sum(scores >= .5)), 'tail_mean': float(scores[max(0,n-100):].mean()),
                'csv': filename}
        image = render_case(output, case, variants)
        behavior = ''
        primary = np.array([r['score'] for r in variants['adaptive']])
        if case['key'] == 'quiet-saw':
            checks = {'onset_peak': float(primary[350:400].max()),
                      'learned_tail_max': float(primary[600:650].max())}
            assert checks['onset_peak'] > .2 and checks['learned_tail_max'] < .1
            assert checks['learned_tail_max'] < .4 * checks['onset_peak']
            item['contract_check'] = checks
            behavior = (f'<p><b>Привыкание:</b> максимум в начале пилы {checks["onset_peak"]:.4f}; '
                        f'максимум в конце продолжающейся пилы {checks["learned_tail_max"]:.4f}.</p>')
        elif case['key'] == 'small-and-large-saw':
            checks = {'small_background_max': float(primary[200:300].max()),
                      'first_large_onset_max': float(primary[350:400].max()),
                      'second_large_onset_max': float(primary[750:790].max()),
                      'recovered_max': float(primary[1100:].max())}
            assert checks['small_background_max'] < .1
            assert min(checks['first_large_onset_max'], checks['second_large_onset_max']) > .8
            assert checks['recovered_max'] < .1
            item['contract_check'] = checks
            behavior = (f'<p><b>Контрольный опыт:</b> малая пила — {checks["small_background_max"]:.4f}; '
                        f'первая большая — {checks["first_large_onset_max"]:.4f}; '
                        f'вторая большая — {checks["second_large_onset_max"]:.4f}; '
                        f'после восстановления — {checks["recovered_max"]:.4f}.</p>'
                        '<p>Малая пила: 100 ↔ 95 (5%). Большая: 100 ↔ 50 (50%) на индексах 350–500 и 750–850.</p>')
        summary.append(item)
        table = ''.join(f'<tr><td>{html.escape(LABELS[key])}</td><td>{v["max_score"]:.4f}</td>'
                        f'<td>{v["samples_ge_05"]}/{v["ready_points"]}</td><td>{v["tail_mean"]:.4f}</td>'
                        f'<td><a href="{v["csv"]}">CSV</a></td></tr>' for key,v in item['variants'].items())
        query = '<pre>' + html.escape(case['query']) + '</pre>' if case.get('query') else ''
        if case['key'].startswith('cpu-'):
            provenance = 'Снимок значений реального PromQL-запроса, сохранённых в Atropos.'
        elif case['key'].startswith('influx-') or case['key'] == 'real-system-image':
            provenance = 'Замороженный реконструированный replay из прежнего отчёта; это не независимая разметка аномалий.'
        else:
            provenance = 'Контролируемый синтетический сценарий с фиксированными исходными значениями.'
        anchor = '<a id="latest-inputs"></a>' if case['key'] == 'influx-scenario-orange' else ''
        sections.append(f'<article id="{case["key"]}">{anchor}<h2>{html.escape(case["label"])}</h2>{query}'
            f'<p>{provenance}</p>{behavior}'
            f'<p>Точек: {item["points"]}; исходные значения сохранены без сглаживания и прореживания.</p>'
            f'<img src="{image}" alt="{html.escape(case["label"])}">'
            '<table><tr><th>Режим</th><th>Max</th><th>Точек ≥0.5 / готовых</th><th>Среднее последних 100</th><th>Данные</th></tr>'
            + table + '</table></article>')
        print(json.dumps({'case': case['key'], 'points': item['points'],
                          'adaptive_max': item['variants']['adaptive']['max_score']}, ensure_ascii=True), flush=True)
    metadata = {'date': '2026-11-22', 'contract': 'abnormal falls/returns + inertia + regime adaptation',
        'class_sha256': hashlib.sha256(inspect.getsource(CusumDetector).encode()).hexdigest(),
        'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'cases': summary}
    (output / 'adaptive-summary.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    intro = '''<!doctype html><html lang="ru"><meta charset="utf-8"><title>CUSUM — аномальные падения и возвраты</title>
<style>body{font:16px/1.55 system-ui;background:#f8fafc;color:#172033;max-width:1160px;margin:36px auto;padding:0 24px}
article{background:white;border:1px solid #dbe2eb;border-radius:12px;padding:20px;margin:26px 0}img{width:100%}
table{border-collapse:collapse;width:100%}td,th{padding:9px;border-bottom:1px solid #dbe2eb;text-align:left}th{background:#f1f5f9}
pre{overflow:auto;white-space:pre-wrap}a{color:#2563eb}.note{background:#eaf0f8;padding:16px;border-radius:8px}</style>
<h1>CUSUM: аномальные падения, возвраты и инерция</h1><p>Пересобрано 22 ноября 2026. Все численные значения получены
повторным причинным прогоном текущего класса. Исходные CSV и снимки не подменялись.</p>
<p class="note">Новая постановка: появление необычных падений с возвратами даёт рост реакции. По мере заполнения
предыдущего фонового окна новым режимом этот режим становится нормой; реакция постепенно затухает.
Обычная малая пила не является аномалией сама по себе, но существенно более сильная пила на её фоне должна выделяться.</p>
<p><a href="#quiet-saw">Появление пилы и привыкание</a> · <a href="#small-and-large-saw">Малая и большая пила</a> ·
<a href="#cpu-30s">CPU 30 с</a> · <a href="#cpu-15s">CPU 15 с</a> · <a href="#cpu-3h-15s">CPU 3 часа / 15 с</a> ·
<a href="adaptive-summary.json">Параметры и SHA256</a></p>
<h2>Что изменено</h2><p>В adaptive-режиме абсолютный drift — 50% фоновой sigma предыдущих дельт,
с нижним допуском relative_drift × |уровень|. Текущее оцениваемое окно в фон не входит.
Прежний 95-й процентиль |дельт| как жёсткий порог каждого движения подавлял реальный CPU до почти нулевой реакции.
95-й процентиль теперь используется только для ограничения редких краёв при оценке sigma.
Прошедшие изменения поступают в фон с задержкой: привычная пила постепенно обучает нормальный режим.
Редкие края фоновой оценки ограничиваются при расчёте sigma, чтобы одна смена уровня
не определяла всю нормировку. Это внутренний расчёт CUSUM, не отдельный детектор MAD.</p>
<p>Сохраняются оба накопителя направлений, относительная глубина и память инерции. Излишек движения сверх
адаптивного допуска питает инерцию. Привычные циклы могут иметь слабый ненулевой балл; новые сильные движения — больший.
После привыкания отклик имеет конечный плавно убывающий хвост в пределах
inertia_window. Число threshold не уменьшалось: в каждом режиме оно равно 5.</p>
<p>В серой линии оставлен прежний sigma-режим (drift=3σ), в красной — adaptive 10/200, в зелёной — adaptive 40/800.
Второй вариант специально показывает влияние более длинной памяти. Порог 0.5 в таблицах — уровень сравнения,
не калиброванная вероятность и не разметка инцидентов. Готовность (ready) отделена от прогрева.</p>
<h2>Почему 3σ — не универсальное правило</h2><p>Для нормального распределения ±3σ покрывает примерно 99,73%,
но нормальность, независимость и известная sigma здесь не обеспечены.
<a href="https://www.itl.nist.gov/div898/handbook/pmc/section5/pmc51.htm">NIST: нормальное распределение</a>.
В классическом CUSUM reference value выбирают относительно искомого сдвига, а не автоматически равным 3σ:
<a href="https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc323.htm">NIST: выбор параметров CUSUM</a>.
Atropos использует специальный score двух направлений с инерцией, а не классическую контрольную карту среднего.</p>
'''
    resolution = json.loads((FIXTURES / 'cusum-cpu-resolution-20261122.json').read_text(encoding='utf-8'))
    resolution.sort(key=lambda c: c['step'])
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(14, 8))
    fine = dict(zip(resolution[0]['times'], resolution[0]['values']))
    comparisons = []
    for axis, case in zip(axes, resolution):
        times = [datetime.fromtimestamp(t/1e9, MSK) for t in case['times']]
        axis.plot(times, case['values'], color='#2563eb', linewidth=1)
        axis.set_title(f'Шаг {case["step"]} с: {len(case["values"])} точек', loc='left', fontsize=11)
        axis.grid(axis='y', color='#e2e8f0')
        delta = max(abs(v-fine[t]) for t,v in zip(case['times'],case['values']) if t in fine)
        assert delta < 1e-12
        comparisons.append({'step': case['step'], 'points': len(case['values']), 'max_common_delta': delta})
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=MSK))
    axes[-1].set_xlabel('22 ноября 2026 · Europe/Moscow')
    fig.suptitle('CPU: один запрос и одно часовое окно, разная сетка выборки', x=.08, ha='left')
    fig.subplots_adjust(left=.08, right=.98, top=.90, bottom=.08, hspace=.45)
    fig.savefig(output / 'adaptive-cpu-resolution.png', dpi=130)
    plt.close(fig)
    metadata['cpu_resolution_check'] = comparisons
    (output / 'adaptive-summary.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    resolution_section = '''<article id="cpu-resolution"><h2>Почему в Atropos были пропущены пики CPU</h2>
<p>Один и тот же сохранённый час запрошен с шагами 5, 15 и 30 секунд: 720, 240 и 120 точек.
На общих timestamps значения совпадают с погрешностью менее 10⁻¹². Дополнительного сглаживания Atropos не делает:
редкая сетка пропускает короткие пики, а график соединяет оставшиеся точки. В текущем источнике указан минимальный
интервал 15 с, в scrape-конфигурации windows-exporter — 5 с. Для просмотра с шагом 5 с нужно также задать этот
минимальный интервал в подключении. Прореживание и ширина графика — разные вещи.</p>
<img src="adaptive-cpu-resolution.png" alt="Различие сеток CPU 5,15,30 секунд"></article>'''
    historical = append_grafana_replay(output)
    historical_section = historical[1] if historical else ''
    (output / 'report.html').write_text(intro + ''.join(sections) + resolution_section + historical_section + '</html>', encoding='utf-8')
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/cusum'))
    args = parser.parse_args()
    metadata = make_report(args.output)
    print(json.dumps({'report': str(args.output/'report.html'), 'cases': len(metadata['cases'])}, ensure_ascii=True))


if __name__ == '__main__':
    main()
