"""Append all four service response series from the user-selected Grafana panel."""

import hashlib
import html
import json
from pathlib import Path
import re
import shutil

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from tests.acf_replay_scenarios import SETTINGS
from tests.acf_service_scenarios import FIXTURES, STEM, load_snapshots
from tests.trend_replay_appendices import MSK, save_csv
from tests.visualize_acf_replays import render_case, replay_case, summarize


def create_services_appendix(output, test_log=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10})
    cases, metadata = load_snapshots()
    summaries, all_results = [], {}
    colors = {'service1': '#73bf69', 'service2': '#e0b400', 'service3': '#e08228', 'service4': '#df4964'}
    for case in cases:
        service = case.name.split('-')[0]
        variants, results = {}, {}
        for key, config in SETTINGS.items():
            rows, peak, index = replay_case(case, config)
            results[key] = (rows, peak, index)
            stem = 'service-acf-'+service+'-'+key
            save_csv(output/(stem+'.csv'), rows)
            save_csv(output/(stem+'-reference.csv'),
                     [{k: r[k] for k in ('index', 'timestamp_utc', 'timestamp_msk', 'value')}
                      for r in rows[:config['reference_window']]])
            save_csv(output/(stem+'-peak-lags.csv'),
                     [{'lag': k, 'lag_seconds': 15*k, 'acf': peak.acf[k],
                       'reference_acf': peak.reference_acf[k], 'change': peak.lag_changes[k],
                       'threshold': peak.thresholds[k], 'significant': k in peak.significant_lags}
                      for k in range(config['max_lag']+1)])
            variants[key] = {**summarize(case, rows, peak, index, config), 'stem': stem}
        render_case(output, case, results)
        summaries.append({'service': service, 'points': len(case.values), 'image': 'replay-'+case.name+'.png',
                          'values_sha256': metadata['services'][service]['values_sha256'], 'variants': variants})
        all_results[service] = (case, results)
    fig, axes = plt.subplots(3, 1, figsize=(14, 11), layout='constrained', sharex=True,
                             gridspec_kw={'height_ratios': [3, 1.7, 1.7]})
    axes[0].set_facecolor('#181d24')
    for service, (case, results) in all_results.items():
        axes[0].plot(case.timestamps, case.values, color=colors[service], lw=.8, label=service)
        for row, key in enumerate(SETTINGS, 1):
            axes[row].plot(case.timestamps,
                           [r['score'] if r['ready'] else np.nan for r in results[key][0]],
                           color=colors[service], lw=.9, label=service)
    axes[0].set_title('Время отклика сервисов — 05.10.2026: те же точки InfluxDB и ACFDetector')
    axes[0].set_ylabel('Время отклика, мс')
    axes[1].set_ylabel('Score: окно 256,\n32 лага, фон 512')
    axes[2].set_ylabel('Score: окно 40,\n10 лагов, фон 160')
    axes[2].xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK, minticks=6, maxticks=10))
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=MSK))
    axes[2].set_xlabel('5 октября 2026, Europe/Moscow (UTC+03:00)')
    for ax in axes:
        ax.grid(alpha=.2)
        ax.legend(fontsize=9, ncol=4)
        ax.set_xlim(cases[0].timestamps[0], cases[0].timestamps[-1])
    fig.savefig(output/'service-acf-overview.png', dpi=120)
    plt.close(fig)
    for path in FIXTURES.glob(STEM+'*'):
        shutil.copy2(path, output/path.name)
    result = {'date': metadata['snapshot_date'], 'source': metadata, 'series_count': len(cases),
              'evaluation_variants': len(cases)*len(SETTINGS), 'total_points': metadata['total_points'],
              'settings': SETTINGS, 'overview_image': 'service-acf-overview.png', 'services': summaries,
              'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if test_log is not None:
        contents = Path(test_log).read_text(encoding='utf-8')
        match = re.search(r'Ran (\d+) tests? in ([\d.]+)s', contents)
        if not match or not re.search(r'^OK(?: \(skipped=\d+\))?\s*$', contents, re.M) or 'FAILED (' in contents:
            raise ValueError('Expected a completed successful unittest log')
        skipped = re.search(r'OK \(skipped=(\d+)\)', contents)
        result['test_run'] = {'tests': int(match[1]), 'seconds': float(match[2]),
                              'skipped': int(skipped[1]) if skipped else 0}
        if Path(test_log).resolve() != (output/'service-acf-tests.txt').resolve():
            shutil.copy2(test_log, output/'service-acf-tests.txt')
    save_csv(output/'service-acf-summary.csv',
             [{'service': item['service'], 'variant': key,
               **{k: v[k] for k in ('points', 'first_ready_msk', 'ready_samples', 'nonzero_samples',
                                    'max_score', 'depth_at_peak_score', 'max_depth', 'peak_msk')}}
              for item in summaries for key, v in item['variants'].items()])
    (output/'service-acf-summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return result


def render_services_appendix(appendix):
    if not appendix:
        return ''
    table = ''.join(
        f'<tr><td><a href="#acf-{item["service"]}">{item["service"]}</a></td><td>{key}</td>'
        f'<td>{v["max_score"]:.5f}</td><td>{v["depth_at_peak_score"]}</td>'
        f'<td>{v["peak_msk"][11:19] if v["max_score"] > 0 else "—"}</td><td>{v["nonzero_samples"]}/{v["ready_samples"]}</td></tr>'
        for item in appendix['services'] for key, v in item['variants'].items())
    check = appendix.get('test_run')
    validation = (f'<p>Проверки ACF: {check["tests"]} тестов, пропущено {check["skipped"]}. '
                  '<a href="service-acf-tests.txt">Журнал</a>.</p>' if check else '')
    result = f'''<section id="service-response-acf"><h2>Время отклика сервисов: панель за 5 октября 2026</h2>
<p>Прогон текущего ACFDetector на четырех рядах service1–service4 из панели 1
<code>service-response-time-20261005</code>. Интервал по ссылке пользователя:
5 октября 2026 00:00:00–11:50:00 МСК, обе границы включены. По 2841 точке через 15 секунд,
всего {appendix['total_points']} исходных значений в миллисекундах.</p>
<p>Снимок из работающей InfluxDB прочитан {html.escape(appendix['date'])}. Измерение
<code>service_response_time</code>, поле <code>value</code>, метка <code>service</code>.
Все значения и timestamps поточечно сверены с ответом Grafana для каждой из четырех кривых.
Происхождение: ранее восстановленный по изображению replay, загруженный в БД.
Запросы читали исходные точки без усреднения и изменения панели.</p>
<p>Default: window=256 (64 минуты), max_lag=32, reference_window=512.
Short: window=40 (10 минут), max_lag=10, reference_window=160.
Threshold=3.5 и min_change=0.1 для обоих. Фон каждого сервиса обучается независимо
по первым M точкам и фиксируется; следующие N точек прогревают отдельное локальное окно.
Первый готовый результат default в 03:11:45, short в 00:49:45 МСК.
На графиках score прогрев показан разрывом. Это прогон существующей калибровки;
полоса на коррелированном фоне может скрывать изменения, как в ранее разобранном RPS.</p>
<p><b>Наблюдаемый результат:</b> default дает нулевой score на всех четырех рядах.
В short ненулевые оценки есть только у service2 (максимум около 0.040) и service3
(около 0.062). Время максимума не указывается для полностью нулевого результата.
Тесты подтверждают правильность расчета по действующей формуле; отсутствие реакции
на этих данных показывает ограниченную чувствительность этой калибровки.</p>
<p>Score оценивает изменение ACF относительно начального участка.
Начальный участок не имеет независимой разметки здорового режима.
Depth — дальность изменившихся связей; момент максимального score не является
оценкой начала события. Ненулевые перекрывающиеся окна не равны числу отдельных инцидентов.</p>
{validation}<table><tr><th>Ряд</th><th>Вариант</th><th>Max score</th><th>Depth на максимуме, лаги</th>
<th>Время максимума МСК</th><th>Ненулевых / готовых</th></tr>{table}</table>
<img src="{appendix['overview_image']}" alt="Четыре исходных ряда сервисов и ACF score двух конфигураций">
<p><a href="{STEM}.csv">Снимок всех исходных значений</a> · <a href="{STEM}.json">Происхождение и SHA256</a> ·
<a href="service-acf-summary.csv">Сводка CSV</a> · <a href="service-acf-summary.json">Результаты JSON</a></p></section>'''
    for item in appendix['services']:
        service = item['service']
        links = ''.join(f'<p>{key}: <a href="{v["stem"]}.csv">все точки, score и depth</a> · '
                        f'<a href="{v["stem"]}-reference.csv">фон</a> · '
                        f'<a href="{v["stem"]}-peak-lags.csv">ACF на максимуме</a></p>'
                        for key, v in item['variants'].items())
        result += f'''<section id="acf-{service}"><h2>ACF: {service}, 5 октября 2026</h2>
<p>У default все score равны нулю. Его ACF на четвертом графике взят из первого
готового окна в 03:11:45 МСК; отдельного обнаруженного максимума нет.
Последний график увеличивает наиболее резкое изменение исходного ряда.</p>
<img src="{item['image']}" alt="{service}: исходный ряд в миллисекундах, score, глубина и ACF">
{links}<p><a href="{STEM}-{service}.flux">Запрос Flux</a> ·
<a href="{STEM}-{service}-raw.csv">Ответ InfluxDB</a></p></section>'''
    return result
