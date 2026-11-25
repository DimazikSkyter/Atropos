"""Replay the four original fixtures and stylized screenshot scenarios."""

import argparse
import csv
import html
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
import numpy as np

from apps.detectors import CusumDetector
from tests.cusum_image_report import append_real_system
from tests.cusum_grafana_report import append_grafana_replay
from tests.cusum_replay_report import append_recent
from tests.cusum_scenarios import (
    legacy_scenarios, legacy_scores, noisy_scenario, saw_scenario, small_dip_scenario,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/cusum'))
    parser.add_argument('--append-replays', action='store_true', help='Add the corrected image and InfluxDB data to an existing report')
    parser.add_argument('--test-log', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.append_replays:
        meta=append_recent(args.output,args.test_log)
        print(json.dumps({'report':str(args.output/'report.html'),'base_scenarios':meta['base_scenarios'],
                          'new_series':meta['series_count'],'points':meta['input_points'],
                          'tests':meta.get('test_run')},ensure_ascii=False,indent=2))
        return
    specs = [(name, data, start, end, True) for name, data, start, end in legacy_scenarios()]
    specs += [('quiet-saw', saw_scenario(), 350, 650, False),
              ('noisy-saw', noisy_scenario(), 350, 650, False),
              ('isolated-small-dip', small_dip_scenario(), 350, 355, False),
              ('repeated-small-dip', small_dip_scenario(50), 350, 600, False),
              ('level-step', np.array([100.] * 500 + [200.] * 500), 500, 501, False),
              ('constant', np.full(1000, 100.), 500, 501, False)]
    titles = {
        'short': 'Исходный тест: короткая просадка',
        'medium': 'Исходный тест: средняя просадка',
        'long': 'Исходный тест: длинная просадка',
        'shallow': 'Исходный тест: неглубокая просадка',
        'quiet-saw': 'Пила на спокойном фоне после смены уровня',
        'noisy-saw': 'Та же пила на фоне высокой дисперсии',
        'isolated-small-dip': 'Одиночная просадка на 5%: низкий балл',
        'repeated-small-dip': 'Много просадок на те же 5%: накопление инерции',
        'level-step': 'Смена уровня без восстановления: отдельный детектор',
        'constant': 'Горизонтальная прямая: оценка 0',
    }
    summary, sections = [], []
    for name, values, start, end, compare in specs:
        detector = CusumDetector()
        scores, sigmas = [], []
        for value in values:
            scores.append(detector.update(value))
            sigmas.append(detector.noise_std)
        scores = np.asarray(scores)
        original = legacy_scores(values) if compare else None
        lower, upper = max(0, start - 40), min(len(values), end + 40)
        region = scores[start:end + detector.window - 1]
        row = {
            'scenario': name, 'event_start': start, 'event_end_exclusive': end,
            'event_max': float(region.max()),
            'event_mean': float(scores[start:end].mean()),
            'event_fraction_above_0_5': float(np.mean(scores[start:end] > 0.5)),
            'event_tail_mean': float(scores[max(start, end - 100):end].mean()),
            'quiet_max': float(scores[200:280].max()),
            'recovered_max': float(scores[end + 20:min(end + 200, len(values))].max()),
            'legacy_event_max': float(original[start:end + 9].max()) if compare else None,
        }
        summary.append(row)
        with (args.output / f'{name}.csv').open('w', encoding='utf-8', newline='') as target:
            writer = csv.writer(target)
            writer.writerow(['point', 'value', 'score', 'legacy_score', 'background_delta_std'])
            for i, value in enumerate(values):
                writer.writerow([i, value, scores[i], original[i] if compare else '', sigmas[i]])
        fig, axes = plt.subplots(3, 1, figsize=(12, 8), layout='constrained')
        fig.suptitle(titles[name])
        axes[0].plot(values, color='#334155', linewidth=1, label='Исходный ряд')
        axes[0].axvspan(start, end, color='#f59e0b', alpha=.18)
        axes[0].legend(loc='upper right')
        axes[1].plot(np.arange(lower, upper), values[lower:upper], color='#334155', linewidth=1)
        axes[1].axvspan(start, end, color='#f59e0b', alpha=.18)
        axes[1].set_ylabel('Участок изменения')
        axes[2].plot(np.arange(lower, upper), scores[lower:upper], color='#dc2626',
                     label='Atropos: окно 10, память инерции 200')
        if compare:
            axes[2].plot(np.arange(lower, upper), original[lower:upper], color='#2563eb',
                         label='Исходный CUSUM: threshold 1, drift 0.01', alpha=.8)
        axes[2].axhline(.5, color='#64748b', linestyle='--', linewidth=.8, label='Балл 0.5')
        if name in ('shallow', 'isolated-small-dip'):
            axes[2].axhline(.2, color='#16a34a', linestyle='--', linewidth=1,
                           label='Допустимый максимум 0.2')
        axes[2].axvspan(start, end, color='#f59e0b', alpha=.12)
        axes[2].set_ylim(0, 1.03)
        axes[2].set_ylabel('Балл дефекта [0, 1]')
        axes[2].set_xlabel('Индекс точки')
        axes[2].legend(loc='upper right', fontsize=8)
        for ax in axes:
            ax.set_ylim(bottom=0)
            ax.grid(alpha=.2)
        fig.savefig(args.output / f'{name}.png', dpi=140)
        plt.close(fig)
        legacy_text = f"; исходный максимум {row['legacy_event_max']:.4f}" if compare else ''
        criterion = '<p>Критерий: максимум не выше 0.2.</p>' if name == 'shallow' else ''
        sections.append(f'<article><h2>{html.escape(titles[name])}</h2>'
                        f'<p>Максимальный балл {row["event_max"]:.4f}{legacy_text}; '
                        f'средний балл в событии {row["event_mean"]:.4f}.</p>'
                        f'{criterion}'
                        f'<img src="{name}.png" alt="{html.escape(titles[name])}">'
                        f'<p><a href="{name}.csv">Данные и оценки CSV</a></p></article>')
    real_row, real_article = append_real_system(args.output)
    summary.append(real_row)
    sections.append(real_article)
    grafana_replay = append_grafana_replay(args.output)
    if grafana_replay is not None:
        grafana_row, grafana_article = grafana_replay
        summary.append(grafana_row)
        sections.append(grafana_article)
    document = ('<!doctype html><html lang="ru"><meta charset="utf-8">'
                '<title>CUSUM: берсты и пила</title><style>'
                'body{font:16px system-ui;background:#f8fafc;color:#0f172a;max-width:1100px;'
                'margin:40px auto;padding:0 20px}article{background:white;padding:20px;'
                'margin:24px 0;border:1px solid #e2e8f0;border-radius:12px}'
                'img{width:100%;height:auto}p{line-height:1.6}a{color:#2563eb}</style>'
                '<h1>CUSUM: оценка берстов и пилы</h1>'
                '<p>Четыре исходных сигнала воспроизведены с точными массивами и seed 43 '
                'из CUSUM_TEST.py проекта AnomalyDetectionResolutionSystem. Синий график '
                'воспроизводит исходную реализацию; красный — новую оценку текущего окна.</p>'
                '<p>Пила и шумный ряд — синтетические иллюстрации присланных изображений, '
                'а не извлечённая из них телеметрия. Горизонтальная линия имеет балл 0; '
                'односторонняя смена уровня сама по себе не считается берстом.</p>'
                '<p>Оценка учитывает фоновый шум, относительную глубину движений и '
                'их накопление. Короткая неглубокая просадка получает низкий балл; '
                'продолжающаяся пила набирает инерцию. Спокойное окно сбрасывает её.</p>'
                '<p>Нормировка использует стандартное отклонение прошлых разностей '
                'и медианный уровень спокойного фона. '
                'Прогрев даёт 0 до готовности фоновой оценки и полного окна. '
                'Балл является эвристической силой дефекта и не калиброванной вероятностью.</p>'
                + ''.join(sections) + '</html>')
    (args.output / 'report.html').write_text(document, encoding='utf-8')
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                             encoding='utf-8')
    append_recent(args.output,args.test_log)
    summary=json.loads((args.output/'summary.json').read_text(encoding='utf-8'))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(args.output / 'report.html')


if __name__ == '__main__':
    main()
