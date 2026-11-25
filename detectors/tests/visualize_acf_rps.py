"""Last report section: the exact RPS replay freshly queried from InfluxDB."""

from datetime import datetime
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
from tests.acf_rps_scenario import FIXTURES, STEM, load_snapshot
from tests.trend_replay_appendices import MSK, save_csv
from tests.visualize_acf_replays import replay_case, summarize


def create_rps_appendix(output, test_log=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    case, metadata = load_snapshot()
    variants, results = {}, {}
    for key, settings in SETTINGS.items():
        rows, peak, index = replay_case(case, settings)
        results[key] = (rows, peak, index)
        stem = 'rps-influx-'+key
        save_csv(output/(stem+'.csv'), rows)
        save_csv(output/(stem+'-reference.csv'),
                 [{k: r[k] for k in ('index', 'timestamp_utc', 'timestamp_msk', 'value')}
                  for r in rows[:settings['reference_window']]])
        save_csv(output/(stem+'-peak-lags.csv'),
                 [{'lag': k, 'lag_seconds': k*15, 'acf': peak.acf[k],
                   'reference_acf': peak.reference_acf[k], 'change': peak.lag_changes[k],
                   'threshold': peak.thresholds[k], 'significant': k in peak.significant_lags}
                  for k in range(settings['max_lag']+1)])
        variants[key] = {**summarize(case, rows, peak, index, settings), 'stem': stem}
    times = mdates.date2num(case.timestamps)
    minimum = int(np.argmin(case.values))
    fig, axes = plt.subplots(5, 1, figsize=(14, 14), layout='constrained',
                             gridspec_kw={'height_ratios': [3, 1.6, 1.3, 2.2, 2.6]})
    fig.suptitle('ACFDetector: RPS из InfluxDB, replay=cusum-real-system-image\n'
                 '22–23 сентября 2026, шаг 15 секунд; две длины окна', fontsize=13)
    axes[0].set_facecolor('#181d24')
    axes[0].plot(times, case.values, color='#86ba65', lw=.8)
    axes[0].set_ylabel('RPS, запросов/с')
    axes[0].set_ylim(0, 1100)
    for key, color, label in [('default', '#c43b32', 'Окно 256, лаги 32, фон 512'),
                               ('short', '#2864b5', 'Окно 40, лаги 10, фон 160')]:
        rows = results[key][0]
        axes[1].plot(times, [r['score'] if r['ready'] else np.nan for r in rows],
                     color=color, lw=.8, label=label)
        axes[2].step(times, [r['depth_seconds'] if r['ready'] else np.nan for r in rows],
                     where='post', color=color, lw=.8, label=label)
    axes[1].set_ylabel('Score [0, 1]')
    axes[1].set_ylim(-.003, max(.05, max(v['max_score'] for v in variants.values())*1.12))
    axes[1].legend(fontsize=9)
    axes[2].set_ylabel('Глубина связи, с')
    axes[2].set_ylim(-8, 500)
    for ax in axes[:3]:
        ax.axvspan(times[0], times[511], color='#94a3b8', alpha=.15)
        ax.axvspan(times[511], times[767], color='#f59e0b', alpha=.10)
        ax.set_xlim(times[0], times[-1])
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK, minticks=5, maxticks=9))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m\n%H:%M', tz=MSK))
        ax.grid(alpha=.2)
    peak = results['default'][1]
    lags = np.arange(1, len(peak.acf))*15
    axes[3].plot(lags, peak.acf[1:], label='ACF при пике score, окно 256')
    axes[3].plot(lags, peak.reference_acf[1:], label='ACF начального фона')
    axes[3].fill_between(lags, np.asarray(peak.reference_acf[1:])-peak.thresholds[1:],
                         np.asarray(peak.reference_acf[1:])+peak.thresholds[1:],
                         color='#94a3b8', alpha=.18, label='Эвристическая полоса')
    axes[3].set_xlabel('Лаг, секунды')
    axes[3].set_ylabel('ACF')
    axes[3].set_ylim(-1.1, 1.1)
    axes[3].grid(alpha=.2)
    axes[3].legend(fontsize=8, ncol=2)
    # Five hours around the deepest dip show both the brief drop and the plateau.
    left, right = max(0, minimum-480), min(len(times), minimum+721)
    axes[4].plot(times[left:right], case.values[left:right], color='#4b8745', lw=1)
    axes[4].scatter(times[minimum], case.values[minimum], color='#c43b32', s=20, zorder=4)
    axes[4].set_ylabel('RPS, запросов/с')
    axes[4].set_title('Краткая просадка и последующее плато крупно', fontsize=11)
    axes[4].xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK, minticks=5, maxticks=10))
    axes[4].xaxis.set_major_formatter(mdates.DateFormatter('%d.%m\n%H:%M', tz=MSK))
    axes[4].set_xlabel('Europe/Moscow, UTC+03:00')
    axes[4].grid(alpha=.2)
    fig.savefig(output/'rps-influx.png', dpi=120)
    plt.close(fig)
    for path in FIXTURES.glob(STEM+'*'):
        shutil.copy2(path, output/path.name)
    result = {'date': metadata['snapshot_date'], 'points': len(case.values), 'source': metadata,
              'minimum_index': minimum, 'minimum_msk': case.timestamps[minimum].astimezone(MSK).isoformat(),
              'image': 'rps-influx.png', 'variants': variants,
              'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if test_log is not None:
        contents = Path(test_log).read_text(encoding='utf-8')
        match = re.search(r'Ran (\d+) tests? in ([\d.]+)s', contents)
        if not match or not re.search(r'^OK(?: \(skipped=\d+\))?\s*$', contents, re.M) or 'FAILED (' in contents:
            raise ValueError('Expected a completed successful RPS unittest log')
        skipped = re.search(r'OK \(skipped=(\d+)\)', contents)
        result['test_run'] = {'tests': int(match[1]), 'seconds': float(match[2]),
                              'skipped': int(skipped[1]) if skipped else 0}
        if Path(test_log).resolve() != (output/'rps-test-results.txt').resolve():
            shutil.copy2(test_log, output/'rps-test-results.txt')
    (output/'rps-summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return result


def render_rps_appendix(result):
    if not result:
        return ''
    source = result['source']
    def msk(value):
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(MSK).strftime('%d.%m.%Y %H:%M:%S')
    table = ''.join(f'<tr><td>{key}</td><td>{v["config"]["window"]}/{v["config"]["max_lag"]}/{v["config"]["reference_window"]}</td>'
                    f'<td>{v["max_score"]:.5f}</td><td>{v["depth_at_peak_score"]}</td>'
                    f'<td>{v["peak_msk"]}</td><td>{v["nonzero_samples"]}/{v["ready_samples"]}</td></tr>'
                    for key, v in result['variants'].items())
    exports = ''.join(f'<p>{key}: <a href="{v["stem"]}.csv">все точки, score и depth</a> · '
                      f'<a href="{v["stem"]}-reference.csv">фон</a> · '
                      f'<a href="{v["stem"]}-peak-lags.csv">ACF и пороги на максимуме</a></p>'
                      for key, v in result['variants'].items())
    check = result.get('test_run')
    validation = (f'<p>Проверка: {check["tests"]} тестов, пропущено {check["skipped"]}. '
                  '<a href="rps-test-results.txt">Журнал тестов</a>.</p>' if check else '')
    return f'''<section id="rps-influx"><h2>Добавлено 9 ноября: RPS из InfluxDB по последнему графику</h2>
<p>Снимок прочитан из работающей InfluxDB {html.escape(result['date'])}. Метрика <code>rps</code>,
поле <code>value</code>, метка <code>replay=cusum-real-system-image</code>, bucket <code>metrics-replay</code>.
Все {result['points']} точек поточечно совпали с ответом Grafana для панели <code>cusum-rps-20260922</code>.
Получение данных выполняло только запросы чтения.</p>
<p>На скриншоте выбран диапазон {msk(source['requested_start_utc'])} — {msk(source['requested_stop_utc'])} МСК.
Фактически в нем есть данные с {msk(source['actual_start_utc'])} до {msk(source['actual_end_utc'])} МСК,
с шагом 15 секунд. Пропущенные начало и конец не дополнялись.
Минимум {source['min_rps']:.2f} RPS в {msk(result['minimum_msk'])} МСК.
Происхождение ряда: ранее восстановленный с изображения replay, загруженный в InfluxDB.</p>
<p>Показаны исходные RPS, балл, глубина связи и ACF. Default: окно 256 точек (64 минуты),
32 лага, фон 512; short: окно 40 точек (10 минут), 10 лагов, фон 160.
Первые M точек обучают фон, следующие N прогревают окно. Threshold=3.5, min_change=0.1.
Фон фиксирован; новые данные не участвуют в оценке прошлого. Пропуски на линии score означают прогрев.
Серым показан фон default, светло-желтым — его локальный прогрев.</p>
<table><tr><th>Вариант</th><th>Окно/лаги/фон</th><th>Max score</th><th>Depth на максимуме, лаги</th>
<th>Время максимума МСК</th><th>Ненулевых / готовых</th></tr>{table}</table>
<p>Наблюдаемый результат: реакция слабая и краткая. Максимальный балл default
{result['variants']['default']['max_score']:.5f}, short {result['variants']['short']['max_score']:.5f}.
Время пика балла указано отдельно от времени минимума RPS: эти моменты не обязаны совпадать.</p>
<p>Большая глубина описывает дальность изменившихся связей. Она не является длительностью просадки.
Баллы сравнивают форму лаговых зависимостей с начальным фоном; сильный разовый провал RPS
сам по себе не обязан давать высокий балл. Оцифровка исходной линии может создавать короткую память.</p>
{validation}<img src="{result['image']}" alt="RPS из InfluxDB, score, глубина, ACF и просадка крупно">
{exports}<p><a href="{STEM}.csv">Исходные значения из БД</a> · <a href="{STEM}-raw.csv">Ответ InfluxDB CSV</a> ·
<a href="{STEM}-query.flux">Запрос Flux</a> · <a href="{STEM}.json">Происхождение и SHA256</a> ·
<a href="rps-summary.json">Результаты JSON</a></p></section>'''
