"""Append the image-derived real-system scenario to the CUSUM comparison report."""

import csv
import html
import json
from pathlib import Path
import shutil

from matplotlib import pyplot as plt
from matplotlib.ticker import FuncFormatter
import numpy as np

from apps.detectors import CusumDetector
from tests.cusum_scenarios import real_system_scenario


def clock_label(minute):
    total = 13 * 60 + int(round(minute))
    return f'{(total // 60) % 24:02d}:{total % 60:02d}'


def replay(values):
    detector = CusumDetector()
    scores, sigmas, levels, memory = [], [], [], []
    for value in values:
        scores.append(detector.update(value))
        sigmas.append(detector.noise_std)
        levels.append(detector.background_level)
        memory.append(len(detector._inertia))
    return np.asarray(scores), sigmas, levels, np.asarray(memory)


def append_real_system(output: Path):
    data, metadata = real_system_scenario()
    minutes, values, center = data['minute'], data['value'], data['center_value']
    scores, sigmas, levels, memory = replay(values)
    center_scores, _, _, _ = replay(center)
    if not (np.all(np.isfinite(scores)) and np.all((0 <= scores) & (scores <= 1))):
        raise ValueError('Invalid real-system replay scores')
    phases = [('first-dip', 'Первая небольшая просадка', 105, 155),
              ('long-entry', 'Вход в длинную просадку, включая узкую иглу', 655, 680),
              ('long-plateau', 'Нижнее плато после затухания входа', 680, 780),
              ('long-recovery', 'Восстановление уровня', 780, 810),
              ('later-spikes', 'Последующий участок с отдельными пиками', 810, len(values))]
    phase_rows = []
    for name, title, start, end in phases:
        phase_rows.append({'phase': name, 'title': title,
                           'start_clock': clock_label(start), 'end_clock': clock_label(end),
                           'start_minute': start, 'end_minute_exclusive': end,
                           'max_score': float(scores[start:end].max()),
                           'mean_score': float(scores[start:end].mean()),
                           'center_max_score': float(center_scores[start:end].max())})
    sensitivity = []
    configured_scale = metadata['assumptions']['units_per_vertical_pixel']
    for units in (0.5, 1.0, 2.0):
        alternative = 1000 + (values - 1000) * units / configured_scale
        alternative_scores, _, _, _ = replay(alternative)
        sensitivity.append({'units_per_vertical_pixel': units,
                            'entry_max': float(alternative_scores[655:680].max()),
                            'plateau_max': float(alternative_scores[680:780].max()),
                            'plateau_level': float(np.median(alternative[680:780]))})
    response = np.flatnonzero(scores[655:680] > 1e-6) + 655
    transient = {'peak': float(scores[655:680].max()),
                 'response_samples': len(response),
                 'response_start_clock': None, 'response_last_clock': None,
                 'score_zero_clock': None, 'inertia_reset_clock': None}
    if len(response):
        last_response = int(response[-1])
        resets = np.flatnonzero(memory[last_response + 1:680] == 0)
        transient.update(response_start_clock=clock_label(response[0]),
                         response_last_clock=clock_label(last_response),
                         score_zero_clock=clock_label(last_response + 1),
                         inertia_reset_clock=clock_label(last_response + 1 + resets[0])
                         if len(resets) else None)
    row = {'scenario': 'real-system-image', 'is_raw_telemetry': False,
           'event_start': 665, 'event_end_exclusive': 795,
           'overall_max': float(scores.max()),
           'event_max': float(scores[665:804].max()),
           'event_mean': float(scores[665:795].mean()),
           'plateau_max': float(scores[680:780].max()),
           'center_overall_max': float(center_scores.max()),
           'sample_step_seconds': 60, 'points': len(values),
           'phases': phase_rows, 'entry_transient': transient,
           'vertical_scale_sensitivity': sensitivity,
           'reconstruction': metadata}
    with (output / 'real-system-image.csv').open('w', encoding='utf-8', newline='') as target:
        writer = csv.writer(target)
        writer.writerow(['minute_from_13_00', 'clock', 'value_with_spikes', 'center_value',
                         'score_with_spikes', 'center_score', 'background_delta_std',
                         'background_level', 'inertia_buffer_samples', 'pixel_x',
                         'envelope_pixel_y', 'center_pixel_y'])
        for i, minute in enumerate(minutes):
            writer.writerow([int(minute), clock_label(minute), values[i], center[i], scores[i],
                             center_scores[i], sigmas[i], levels[i], int(memory[i]), data['pixel_x'][i],
                             data['envelope_pixel_y'][i], data['center_pixel_y'][i]])

    fig, axes = plt.subplots(4, 1, figsize=(13, 11), layout='constrained')
    fig.suptitle('Реальная система: приближённый зелёный ряд, уровень 1000, шаг 1 мин')
    axes[0].plot(minutes, values, color='#15803d', linewidth=.9, label='С сохранением узких пиков')
    axes[0].plot(minutes, center, color='#334155', linewidth=.7, alpha=.7, label='Центр линии')
    axes[0].set_ylabel('Условные значения')
    axes[1].plot(minutes, scores, color='#dc2626', label='CUSUM с узкими пиками')
    axes[1].plot(minutes, center_scores, color='#2563eb', linestyle='--', label='CUSUM центра линии')
    zoom = (minutes >= 620) & (minutes <= 830)
    axes[2].plot(minutes[zoom], values[zoom], color='#15803d', linewidth=1)
    axes[2].plot(minutes[zoom], center[zoom], color='#334155', linewidth=.7, alpha=.7)
    axes[2].set_ylabel('Длинная просадка')
    axes[3].plot(minutes[zoom], scores[zoom], color='#dc2626', linewidth=1.2)
    axes[3].plot(minutes[zoom], center_scores[zoom], color='#2563eb', linestyle='--')
    for index, axis in enumerate(axes):
        axis.axvspan(665, 795, color='#f59e0b', alpha=.13)
        if index < 2:
            axis.axvspan(105, 155, color='#0ea5e9', alpha=.1)
            axis.set_xticks(np.arange(0, len(values), 180))
        else:
            axis.set_xticks(np.arange(630, 831, 30))
        axis.xaxis.set_major_formatter(FuncFormatter(lambda value, pos: clock_label(value)))
        axis.set_ylim(bottom=0)
        axis.grid(alpha=.2)
    for axis in (axes[1], axes[3]):
        axis.axhline(.5, color='#64748b', linestyle='--', linewidth=.8)
        axis.set_ylim(0, 1.025)
        axis.set_ylabel('Балл [0, 1]')
    axes[0].legend(loc='lower left', fontsize=8)
    axes[1].legend(loc='upper left', fontsize=8)
    axes[3].set_xlabel('Время по изображению; после 00:00 — следующий день, дата неизвестна')
    fig.savefig(output / 'real-system-image.png', dpi=140)
    plt.close(fig)

    fixtures = Path(__file__).with_name('fixtures')
    shutil.copyfile(fixtures / 'real-system-source.png', output / 'real-system-source.png')
    (output / 'real-system-reconstruction.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    table_rows = ''.join(f'<tr><td>{html.escape(p["title"])}</td>'
                         f'<td>{p["start_clock"]}–{p["end_clock"]}</td>'
                         f'<td>{p["max_score"]:.5f}</td>'
                         f'<td>{p["center_max_score"]:.5f}</td></tr>' for p in phase_rows)
    scale_rows = ''.join(f'<tr><td>{p["units_per_vertical_pixel"]:g}</td>'
                         f'<td>{p["plateau_level"]:.1f}</td><td>{p["entry_max"]:.4f}</td>'
                         f'<td>{p["plateau_max"]:.5f}</td></tr>' for p in sensitivity)
    transient_text = ('<p>Заметного отклика на иглу при этом вертикальном масштабе нет.</p>')
    if len(response):
        reset_text = transient['inertia_reset_clock'] or 'не найдено до конца участка входа'
        transient_text = (f'<p>Отклик на иглу: {transient["response_samples"]} минутных отсчётов '
                          f'({transient["response_start_clock"]}–{transient["response_last_clock"]}). '
                          f'Балл равен нулю с {transient["score_zero_clock"]}; память инерции '
                          f'обнулена к {reset_text}.</p>')
    article = ('<article id="real-system-image"><h2>График реальной системы: длинная смена уровня</h2>'
               '<p>Вход — приложенное изображение. Использован только зелёный ряд; '
               'жёлтая линия и красная рамка в значения не входят. '
               'Подтверждены уровень 1000 и шаг 1 минута. Это приближённая оцифровка '
               'изображения, исходные значения метрики отсутствуют.</p>'
               f'<p><strong>Шкала Y неизвестна.</strong> Принято 1 вертикальный пиксель = {configured_scale:g} единиц. '
               'Это допущение определяет глубину просадок, не результат измерения. '
               'Положение времени восстановлено по отметкам 13:00 и 10:00 следующего дня; '
               'дата неизвестна. Разрывы цветовой линии интерполированы.</p>'
               '<p>Два варианта чтения: центр наиболее яркой зелёной линии и нижняя граница '
               'с сохранением узких пиков. Центр может пропустить иглы, нижняя граница '
               'может принять вертикальные соединительные штрихи за отдельные точки. '
               'Настройки детектора сохранены: окно 10 точек, память 200 разностей.</p>'
               '<img src="real-system-source.png" alt="Исходный график реальной системы">'
               '<img src="real-system-image.png" alt="Восстановленный зелёный ряд и баллы CUSUM">'
               '<p>Первая небольшая просадка даёт почти нулевой балл. '
               'Двухчасовое нижнее плато и восстановление уровня также дают ноль. '
               'При сохранении глубокой иглы на входе появляется отдельная короткая '
               f'реакция до {row["overall_max"]:.4f}; она затухает, а не переносится на часы плато.</p>'
               f'{transient_text}'
               '<table style="width:100%;text-align:left;border-collapse:collapse">'
               '<thead><tr><th>Участок</th><th>Время</th><th>Максимум с пиками</th>'
               '<th>Максимум центра</th></tr></thead><tbody>' + table_rows + '</tbody></table>'
               '<h3>Влияние неизвестного масштаба Y</h3><p>Те же пиксели и шаг времени; '
               'меняется только условное количество единиц в вертикальном пикселе.</p>'
               '<table style="width:100%;text-align:left;border-collapse:collapse">'
               '<thead><tr><th>Единиц/пиксель</th><th>Уровень нижнего плато</th>'
               '<th>Максимум входа</th><th>Максимум плато</th></tr></thead><tbody>'
               + scale_rows + '</tbody></table>'
               '<p><a href="real-system-image.csv">Восстановленные значения и баллы CSV</a> · '
               '<a href="real-system-reconstruction.json">Допущения оцифровки JSON</a></p></article>')
    return row, article
