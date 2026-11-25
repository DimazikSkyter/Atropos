"""MADDetector on exactly the four service curves already replayed through ACF."""

import argparse
import hashlib
import inspect
import json
from pathlib import Path
import re
import shutil

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from apps.detectors import MadDetector
from tests.acf_service_scenarios import FIXTURES, STEM, load_snapshots
from tests.trend_replay_appendices import MSK, save_csv


CONFIG = {'window': 20, 'data_max_size': 100}
COLORS = {'service1': '#73bf69', 'service2': '#e0b400', 'service3': '#e08228', 'service4': '#df4964'}


def replay_mad(case, config=None):
    detector = MadDetector(**(CONFIG if config is None else config))
    rows = []
    for index, (value, stamp) in enumerate(zip(case.values, case.timestamps)):
        score = detector.update(float(value))
        reference = detector.reference_mad
        local = detector.local_mad
        relative = (abs(local-reference)/reference if reference else
                    (0.0 if local == 0 else None)) if detector.ready else None
        rows.append({'index': index, 'timestamp_utc': stamp.isoformat().replace('+00:00', 'Z'),
                     'timestamp_msk': stamp.astimezone(MSK).isoformat(), 'value_ms': float(value),
                     'ready': detector.ready, 'score': score, 'local_mad_ms': local,
                     'reference_mad_ms': reference, 'relative_deviation': relative})
    return rows


def describe(case, rows):
    ready = [r for r in rows if r['ready']]
    peak = max(ready, key=lambda r: r['score'])
    segments = []
    for label, start, stop in (('00:00–05:00', 0, 1200), ('05:00–10:00', 1200, 2400),
                               ('10:00–11:50', 2400, len(rows))):
        selected = [r for r in rows[start:stop] if r['ready']]
        segments.append({'interval_msk': label, 'ready_samples': len(selected),
                         'mean_score': float(np.mean([r['score'] for r in selected])),
                         'max_score': max(r['score'] for r in selected)})
    return {'service': case.name.split('-')[0], 'points': len(rows), 'ready_samples': len(ready),
            'first_ready_index': ready[0]['index'], 'first_ready_msk': ready[0]['timestamp_msk'],
            'max_score': peak['score'], 'peak_msk': peak['timestamp_msk'], 'peak_index': peak['index'],
            'mean_score': float(np.mean([r['score'] for r in ready])),
            'local_mad_at_peak': peak['local_mad_ms'], 'reference_mad_at_peak': peak['reference_mad_ms'],
            'input_values_sha256': hashlib.sha256(case.values.astype('<f8').tobytes()).hexdigest(),
            'segments': segments}


def time_axis(ax):
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK, minticks=6, maxticks=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=MSK))
    ax.grid(alpha=.2)


def create_report(output, test_log=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10})
    cases, source = load_snapshots()
    summaries, replayed = [], {}
    fig, axes = plt.subplots(2, 1, figsize=(14, 9), layout='constrained', sharex=True,
                             gridspec_kw={'height_ratios': [3, 2]})
    axes[0].set_facecolor('#181d24')
    for case in cases:
        rows = replay_mad(case)
        info = describe(case, rows)
        service = info['service']
        stem = 'mad-'+service
        info.update({'csv': stem+'.csv', 'image': stem+'.png'})
        save_csv(output/(stem+'.csv'), rows)
        replayed[service] = (case, rows)
        summaries.append(info)
        axes[0].plot(case.timestamps, case.values, lw=.8, color=COLORS[service], label=service)
        axes[1].plot(case.timestamps, [r['score'] if r['ready'] else np.nan for r in rows],
                     lw=.8, color=COLORS[service], label=service)
    axes[0].set_title('Время отклика сервисов — 05.10.2026: те же данные, MadDetector 20/100')
    axes[0].set_ylabel('Время отклика, мс')
    axes[1].set_ylabel('MAD score [0, 1]')
    axes[1].set_ylim(-.01, 1.01)
    axes[1].set_xlabel('5 октября 2026, Europe/Moscow (UTC+03:00)')
    for ax in axes:
        time_axis(ax)
        ax.set_xlim(cases[0].timestamps[0], cases[0].timestamps[-1])
        ax.legend(fontsize=9, ncol=4)
    fig.savefig(output/'mad-services-overview.png', dpi=120)
    plt.close(fig)
    for service, (case, rows) in replayed.items():
        fig, axes = plt.subplots(3, 1, figsize=(14, 10), layout='constrained', sharex=True,
                                 gridspec_kw={'height_ratios': [3, 2, 2]})
        axes[0].set_facecolor('#181d24')
        axes[0].plot(case.timestamps, case.values, color=COLORS[service], lw=.8)
        axes[0].set_title(f'{service}: MadDetector(window=20, data_max_size=100), 5 октября 2026')
        axes[0].set_ylabel('Время отклика, мс')
        axes[1].plot(case.timestamps, [r['score'] if r['ready'] else np.nan for r in rows], color='#b84336', lw=1)
        axes[1].set_ylabel('MAD score [0, 1]')
        axes[1].set_ylim(-.01, 1.01)
        axes[2].plot(case.timestamps, [r['local_mad_ms'] for r in rows], lw=.9, label='Локальный MAD, 20 точек')
        axes[2].plot(case.timestamps, [r['reference_mad_ms'] for r in rows], lw=.9, label='Референсный MAD, 100 точек')
        axes[2].set_ylabel('Отклонение, мс')
        axes[2].set_xlabel('Europe/Moscow (UTC+03:00)')
        axes[2].legend(fontsize=9)
        for ax in axes:
            ax.axvspan(case.timestamps[0], case.timestamps[98], color='#94a3b8', alpha=.15)
            time_axis(ax)
            ax.set_xlim(case.timestamps[0], case.timestamps[-1])
        fig.savefig(output/('mad-'+service+'.png'), dpi=120)
        plt.close(fig)
    for path in FIXTURES.glob(STEM+'*'):
        shutil.copy2(path, output/path.name)
    summary = {'date': source['snapshot_date'], 'detector': 'MadDetector', 'config': CONFIG,
               'sample_interval_seconds': 15, 'source': source, 'services': summaries,
               'detector_class_sha256': hashlib.sha256(inspect.getsource(MadDetector).encode()).hexdigest()}
    if test_log is not None:
        contents = Path(test_log).read_text(encoding='utf-8')
        match = re.search(r'Ran (\d+) tests? in ([\d.]+)s', contents)
        if not match or not re.search(r'^OK(?: \(skipped=\d+\))?\s*$', contents, re.M) or 'FAILED (' in contents:
            raise ValueError('Expected a completed successful unittest log')
        skipped = re.search(r'OK \(skipped=(\d+)\)', contents)
        summary['test_run'] = {'tests': int(match[1]), 'seconds': float(match[2]),
                               'skipped': int(skipped[1]) if skipped else 0}
        shutil.copy2(test_log, output/'test-results.txt')
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    save_csv(output/'summary.csv', [{k: v[k] for k in ('service', 'points', 'ready_samples', 'max_score',
             'peak_msk', 'mean_score', 'local_mad_at_peak', 'reference_mad_at_peak')} for v in summaries])
    table = ''.join(f'<tr><td><a href="#{v["service"]}">{v["service"]}</a></td><td>{v["max_score"]:.5f}</td>'
                    f'<td>{v["peak_msk"][11:19]}</td><td>{v["segments"][0]["mean_score"]:.5f}</td>'
                    f'<td>{v["segments"][1]["mean_score"]:.5f}</td><td>{v["segments"][2]["mean_score"]:.5f}</td></tr>'
                    for v in summaries)
    sections = ''.join(f'<section id="{v["service"]}"><h2>{v["service"]}</h2>'
                       f'<img src="{v["image"]}" alt="{v["service"]}: исходные миллисекунды, MAD score и два отклонения">'
                       f'<p><a href="{v["csv"]}">Все точки, ready, score, локальный и референсный MAD</a></p></section>'
                       for v in summaries)
    check = summary.get('test_run')
    validation = (f'<p>Проверка: {check["tests"]} тестов, пропущено {check["skipped"]}. '
                  '<a href="test-results.txt">Журнал</a>.</p>' if check else '')
    lower_peaks = ', '.join(v['service'] for v in summaries
                            if v['local_mad_at_peak'] < v['reference_mad_at_peak'])
    peak_note = (f'<p>У {lower_peaks} максимальный балл соответствует уменьшению локального MAD '
                 'относительно референса. Поэтому высокое значение score здесь само по себе '
                 'не означает рост разброса.</p>' if lower_peaks else '')
    document = f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>MadDetector: время отклика сервисов</title>
<style>body{{font:16px/1.55 system-ui,sans-serif;max-width:1160px;margin:36px auto;padding:0 24px;color:#182a3a}}
table{{border-collapse:collapse;width:100%;margin:20px 0}}td,th{{border:1px solid #d2dce4;padding:8px;text-align:left}}
th{{background:#e9f0f5}}img{{width:100%;height:auto}}pre{{background:#edf1f4;padding:16px;overflow:auto}}
section{{border-top:1px solid #cad6df;margin-top:40px;padding-top:20px}}a{{color:#20598c}}</style>
<h1>MadDetector: время отклика сервисов 5 октября 2026</h1>
<p>Дата прогона: 9 ноября 2026. Ровно те же 11 364 исходных значения,
что использованы в <a href="../acf/report.html#service-response-acf">прогоне ACF</a>:
service1–service4, по 2841 точке с шагом 15 секунд за 00:00–11:50 МСК.</p>
<p>Снимок из InfluxDB поточечно сверен с Grafana. Измерение service_response_time,
поле value, метка service. Происхождение — сохраненный в БД replay, восстановленный по изображению.
Этот прогон использует сохраненный снимок с проверкой SHA256; значения и временные метки совпадают с ACF.</p>
<p>Параметры существующего MadDetector по умолчанию: window=20 (5 минут), data_max_size=100 (25 минут).
Один экземпляр на сервис. Первый готовый результат — 00:24:45 МСК, индекс 99;
первые 99 точек — прогрев, на графике score они пропущены. Референс скользящий и включает локальное окно.</p>
<pre>MAD(x) = mean(abs(x - mean(x)))
local = MAD(последние 20 точек)
reference = MAD(последние 100 точек)
score = tanh(0.5 * abs(local-reference) / reference)</pre>
<p>MAD здесь — среднее абсолютное отклонение от среднего. При нулевом референсе
и нулевом локальном MAD score=0. Балл отражает отличие локального разброса от референсного,
растет как при увеличении, так и при уменьшении разброса. Это не вероятность;
порог бинарного алерта для прогона не назначается.</p>
<p>Локальное окно короче обычного цикла в этих данных, поэтому score может колебаться
на регулярном раннем участке. Сравнивать реакцию на поздние изменения нужно с этим фоном,
а не только с максимальным значением. Средние баллы приведены для трех часовых интервалов;
интервалы служат описанию данных и не являются эталонной разметкой аномалий.</p>
{validation}<table><tr><th>Сервис</th><th>Max score</th><th>Время max МСК</th>
<th>Средний score 00–05</th><th>Средний 05–10</th><th>Средний 10–11:50</th></tr>{table}</table>
{peak_note}
<img src="mad-services-overview.png" alt="Четыре исходных ряда и MAD score">
<p><a href="summary.csv">Сводка CSV</a> · <a href="summary.json">Результаты JSON</a> ·
<a href="{STEM}.csv">Исходный снимок, общий с ACF</a> · <a href="{STEM}.json">Происхождение и SHA256</a></p>
<pre>python -m unittest tests.test_mad tests.test_mad_services -v
python -m tests.visualize_mad_services --output artifacts/mad</pre>{sections}</html>'''
    (output/'report.html').write_text(document, encoding='utf-8')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/mad'))
    parser.add_argument('--test-log', type=Path)
    parser.add_argument('--remove-cycle', action='store_true', help='Also learn a cycle on the first three hours and replay residuals')
    args = parser.parse_args()
    summary = create_report(args.output, args.test_log)
    if args.remove_cycle:
        from tests.visualize_mad_deseasonalized import append_cycle_removal
        summary = append_cycle_removal(args.output, args.test_log)
    print(json.dumps({'report': str(args.output.resolve()/'report.html'), 'config': CONFIG,
                      'services': [{k: v[k] for k in ('service', 'max_score', 'peak_msk', 'mean_score', 'segments')}
                                   for v in summary['services']]}, ensure_ascii=False, indent=2))
