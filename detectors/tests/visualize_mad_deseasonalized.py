"""Append before/after MAD results with a frozen, prefix-trained cycle removed."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from tests.acf_service_scenarios import load_snapshots
from tests.mad_cycle_scenarios import MAD_CONFIG, TRAIN_SAMPLES, describe_comparison, replay_without_cycle
from tests.trend_replay_appendices import MSK, save_csv


COLORS = {'service1': '#73bf69', 'service2': '#e0b400', 'service3': '#e08228', 'service4': '#df4964'}
BEGIN = '<!-- MAD_CYCLE_REMOVAL_BEGIN -->'
END = '<!-- MAD_CYCLE_REMOVAL_END -->'
NAV = '<p><a href="#without-cycle">Новый прогон: MAD после удаления цикла</a></p>'


def configure_time(ax, times):
    ax.set_xlim(times[0], times[-1])
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=MSK, minticks=6, maxticks=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=MSK))
    ax.axvspan(times[0], times[719], color='#94a3b8', alpha=.13)
    ax.axvspan(times[720], times[818], color='#f59e0b', alpha=.10)
    ax.grid(alpha=.2)


def create_cycle_appendix(output, test_log=None):
    output = Path(output)
    cases, source = load_snapshots()
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10})
    summaries = []
    fig, axes = plt.subplots(4, 1, figsize=(14, 14), layout='constrained', sharex=True,
                             gridspec_kw={'height_ratios': [3, 3, 1.8, 1.8]})
    axes[0].set_facecolor('#181d24')
    for case in cases:
        service = case.name.split('-')[0]
        rows, model = replay_without_cycle(case)
        info = describe_comparison(rows, model)
        info.update({'service': service, 'stem': 'cycle-'+service,
                     'input_values_sha256': hashlib.sha256(case.values.astype('<f8').tobytes()).hexdigest()})
        summaries.append(info)
        save_csv(output/(info['stem']+'.csv'), rows)
        (output/(info['stem']+'-model.json')).write_text(json.dumps(info['model'], indent=2), encoding='utf-8')
        save_csv(output/(info['stem']+'-template.csv'),
                 [{'phase_fraction': phase, 'cycle_ms': value, 'healthy_value_ms': value+model.baseline_ms}
                  for phase, value in zip(model.phase_points, model.cycle_values_ms)])
        times = case.timestamps
        raw_score = [r['raw_score'] if r['ready'] else np.nan for r in rows]
        clean_score = [r['residual_score'] if r['ready'] else np.nan for r in rows]
        residuals = [r['residual_ms'] for r in rows]
        axes[0].plot(times, case.values, color=COLORS[service], lw=.7, label=service)
        axes[1].plot(times, residuals, color=COLORS[service], lw=.7, label=service)
        axes[2].plot(times, raw_score, color=COLORS[service], lw=.7, label=service)
        axes[3].plot(times, clean_score, color=COLORS[service], lw=.7, label=service)
        detail, ax = plt.subplots(4, 1, figsize=(14, 13), layout='constrained', sharex=True,
                                  gridspec_kw={'height_ratios': [3, 2, 2, 2]})
        ax[0].set_title(f'{service}: вычитание здорового цикла ({model.period_samples*.25:.2f} мин) и MAD 20/100')
        ax[0].plot(times, case.values, color=COLORS[service], lw=.8, label='Исходные данные')
        ax[0].plot(times, [r['healthy_prediction_ms'] for r in rows], color='#4967c8', lw=.7,
                   alpha=.8, label='Замороженный здоровый шаблон')
        ax[0].set_ylabel('Время отклика, мс')
        ax[0].legend(fontsize=8, ncol=2)
        ax[1].plot(times, residuals, color='#285b80', lw=.8)
        ax[1].axhline(0, color='#777', lw=.5)
        ax[1].set_ylabel('Остаток, мс')
        ax[2].plot(times, raw_score, color='#999', alpha=.65, lw=.8, label='MAD исходного ряда')
        ax[2].plot(times, clean_score, color='#bf443a', lw=.9, label='MAD остатка')
        ax[2].set_ylim(-.01, 1.01)
        ax[2].set_ylabel('Score [0, 1]')
        ax[2].legend(fontsize=8, ncol=2)
        ax[3].plot(times, [r['residual_local_mad_ms'] for r in rows], label='Локальный MAD остатка, 20 точек', lw=.8)
        ax[3].plot(times, [r['residual_reference_mad_ms'] for r in rows], label='Референсный MAD остатка, 100 точек', lw=.8)
        ax[3].set_ylabel('MAD остатка, мс')
        ax[3].legend(fontsize=8, ncol=2)
        ax[3].set_xlabel('5 октября 2026, Europe/Moscow (UTC+03:00)')
        for chart in ax:
            configure_time(chart, times)
        detail.savefig(output/(info['stem']+'.png'), dpi=120)
        plt.close(detail)
    axes[0].set_title('Тот же график: исходные ряды, остатки и MAD до/после удаления цикла')
    axes[0].set_ylabel('Исходные значения, мс')
    axes[1].set_ylabel('Остатки, мс')
    axes[2].set_ylabel('MAD исходного ряда')
    axes[3].set_ylabel('MAD остатка')
    for ax in axes[2:]:
        ax.set_ylim(-.01, 1.01)
    for ax in axes:
        configure_time(ax, cases[0].timestamps)
        ax.legend(fontsize=8, ncol=4)
    axes[-1].set_xlabel('5 октября 2026, Europe/Moscow (UTC+03:00)')
    fig.savefig(output/'cycle-overview.png', dpi=120)
    plt.close(fig)
    result = {'train_samples': TRAIN_SAMPLES, 'train_interval_msk': '00:00:00–02:59:45',
              'mad_config': MAD_CONFIG, 'first_ready_index': 819, 'first_ready_msk': '2026-10-05T03:24:45+03:00',
              'source': source, 'services': summaries,
              'method': 'Fractional prefix-only period; 64-bin robust phase template; fixed forecast; no future fitting'}
    if test_log is not None:
        contents = Path(test_log).read_text(encoding='utf-8')
        match = re.search(r'Ran (\d+) tests? in ([\d.]+)s', contents)
        if not match or not re.search(r'^OK(?: \(skipped=\d+\))?\s*$', contents, re.M) or 'FAILED (' in contents:
            raise ValueError('Expected a completed successful unittest log')
        skipped = re.search(r'OK \(skipped=(\d+)\)', contents)
        result['test_run'] = {'tests': int(match[1]), 'seconds': float(match[2]),
                              'skipped': int(skipped[1]) if skipped else 0}
        if Path(test_log).resolve() != (output/'cycle-tests.txt').resolve():
            shutil.copy2(test_log, output/'cycle-tests.txt')
    (output/'cycle-summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    save_csv(output/'cycle-summary.csv',
             [{'service': info['service'], 'period_minutes': info['period_minutes'], **segment}
              for info in summaries for segment in info['segments']])
    return result


def render_cycle_appendix(result):
    table = ''.join(f'<tr><td><a href="#cycle-{v["service"]}">{v["service"]}</a></td>'
                    f'<td>{v["period_minutes"]:.3f}</td><td>{v["segments"][0]["raw_mean_score"]:.3f} → '
                    f'{v["segments"][0]["residual_mean_score"]:.3f}</td><td>{v["raw_max_score"]:.3f} → '
                    f'{v["residual_max_score"]:.3f}</td></tr>' for v in result['services'])
    check = result.get('test_run')
    validation = (f'<p>Проверка: {check["tests"]} тестов, пропущено {check["skipped"]}. '
                  '<a href="cycle-tests.txt">Журнал</a>.</p>' if check else '')
    content = f'''<section id="without-cycle"><h2>Эксперимент: удалить обычный цикл и повторить MAD</h2>
<p>Те же четыре исходных ряда и MAD 20/100. Первые 720 точек (00:00–02:59:45 МСК)
используются как предположительно регулярный обучающий участок. По ним отдельно для каждого
сервиса оценивается дробный период, затем фазовый шаблон по медианам 64 групп.
Период уточняется гармонической регрессией с шестью гармониками и усеченной ошибкой.
Между фазами используется круговая линейная интерполяция.</p>
<p>Вычитается прогноз нормального уровня и цикла: остаток = исходное значение − здоровый шаблон.
После обучения модель фиксирована. Последующие изменения уровня, амплитуды и формы цикла
остаются в остатке. Числа периода и коэффициенты определяются только первыми тремя часами.</p>
<p>Для честного сравнения два новых экземпляра MAD начинают получать исходные значения и остатки
одновременно в 03:00. Первый готовый результат у обоих в 03:24:45 МСК.
Средние score раннего участка считаются на <b>не использованных при обучении</b> точках
03:24:45–05:00. Серый участок — обучение шаблона, светло-желтый — прогрев MAD.
Остаток на обучающем участке показан как диагностика подгонки и не подается детектору.</p>
<table><tr><th>Сервис</th><th>Период, минут</th><th>Средний score 03:24–05:00: до → после</th>
<th>Max score после прогрева: до → после</th></tr>{table}</table>
<p>После удаления цикла ранний средний score снижается. Несколько переходов у service1–service3
становятся заметнее, но на раннем участке service1 тоже остается отдельный большой пик.
Последующий рост абсолютного разброса не превращается в устойчивый рост score:
оба MAD по-прежнему адаптируются и делятся друг на друга. Это эксперимент с предобработкой;
формула и параметры MadDetector сохранены.</p>
<p>Замороженный шаблон удаляет приближенно исходную цикличность. Дрейф фазы или периода
дает остаток вместе с изменениями уровня и шума; для этих восстановленных по изображению
данных нет независимой разметки аномалий.</p>
{validation}<img src="cycle-overview.png" alt="Исходные ряды, остатки, score до и после удаления цикла">
<p><a href="cycle-summary.json">Модели и результаты JSON</a> · <a href="cycle-summary.csv">Сравнение по интервалам CSV</a></p></section>'''
    for info in result['services']:
        intervals = ''.join(f'<tr><td>{s["interval_msk"]}</td><td>{s["raw_mean_score"]:.4f} → '
                            f'{s["residual_mean_score"]:.4f}</td><td>{s["raw_std_ms"]:.3f} → '
                            f'{s["residual_std_ms"]:.3f}</td></tr>' for s in info['segments'])
        content += f'''<section id="cycle-{info['service']}"><h2>{info['service']}: после удаления цикла</h2>
<p>Период {info['period_minutes']:.3f} минуты. Максимум MAD остатка {info['residual_max_score']:.4f}
в {info['residual_peak_msk'][11:19]} МСК.</p>
<table><tr><th>Интервал МСК</th><th>Средний score: до → после</th><th>Стандартное отклонение, мс: до → после</th></tr>{intervals}</table>
<img src="{info['stem']}.png" alt="Шаблон цикла, остаток, MAD score и отклонения {info['service']}">
<p><a href="{info['stem']}.csv">Все значения, остатки, готовность, score и MAD до/после</a> ·
<a href="{info['stem']}-model.json">Модель цикла</a> · <a href="{info['stem']}-template.csv">Фазовый шаблон</a></p></section>'''
    return content


def append_cycle_removal(output, test_log=None):
    output = Path(output).resolve()
    summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
    result = create_cycle_appendix(output, test_log)
    if result['source'] != summary['source']:
        raise ValueError('Base report and cycle experiment use different inputs')
    summary['cycle_removal'] = result
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    document = (output/'report.html').read_text(encoding='utf-8')
    document = re.sub(re.escape(BEGIN)+r'.*?'+re.escape(END), '', document, flags=re.S)
    if NAV not in document:
        document = document.replace('</h1>', '</h1>'+NAV, 1)
    if document.count('</html>') != 1:
        raise ValueError('Expected one complete base MAD report')
    document = document.replace('</html>', BEGIN+render_cycle_appendix(result)+END+'</html>')
    (output/'report.html').write_text(document, encoding='utf-8')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/mad'))
    parser.add_argument('--test-log', type=Path)
    args = parser.parse_args()
    summary = append_cycle_removal(args.output, args.test_log)
    print(json.dumps({'report': str(args.output.resolve()/'report.html'),
                      'services': [{k: v[k] for k in ('service', 'period_minutes', 'raw_max_score', 'residual_max_score')}
                                   for v in summary['cycle_removal']['services']]}, indent=2))
