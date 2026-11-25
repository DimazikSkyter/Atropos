"""Build Russian benchmark tables from measured raw JSON, never from constants."""
import csv
import gzip
import html
import json
import math
import re
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAMES = ('Trend', 'CUSUM', 'MAD', 'SSA', 'ACF', 'Correlation', 'PredictionInterval', 'RuntimeTrend')
RESERVE = .7


def number(value, digits=0):
    if value is None:
        return '—'
    if digits:
        return f'{value:,.{digits}f}'.replace(',', ' ').replace('.', ',')
    return f'{round(value):,}'.replace(',', ' ')


def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |',
                      *['| ' + ' | '.join(str(value) for value in row) + ' |' for row in rows]])


def csv_file(name, rows):
    if not rows:
        return
    with (ROOT / name).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def workload_counts(payload):
    metrics = payload['metrics']
    timestamps = {uid: [set(series['times']) for series in metric['series']] for uid, metric in metrics.items()}
    scalar = 0
    pair_points, pairs = 0, 0
    per_detector = {name: 0 for name in NAMES}
    for configured in payload['account_metrics']:
        uid = configured['id']
        if uid not in metrics:
            continue
        count = sum(len(series['times']) for series in metrics[uid]['series'])
        scalar += count
        for name in configured['detectors']:
            if name != 'Correlation':
                per_detector[name] += count
        if 'Correlation' in configured['detectors']:
            related = [(True, values) for values in timestamps[uid]]
            for peer in configured.get('peers', []):
                related.extend((False, values) for values in timestamps.get(peer, []))
            for left_index, (target, left) in enumerate(related):
                if target:
                    for _, right in related[left_index + 1:]:
                        pairs += 1
                        pair_points += len(left & right)
    per_detector['Correlation'] = pair_points
    return {'source_points': scalar, 'correlation_pairs': pairs,
            'correlation_vector_updates': pair_points, 'updates_by_detector': per_detector}


def main():
    rows = [json.loads(path.read_text(encoding='utf-8')) for path in sorted((ROOT / 'results').glob('*.json'))
            if not path.name.startswith('case-')]
    by_id = {row['case']['id']: row for row in rows}
    machine = json.loads((ROOT / 'machine.json').read_text(encoding='utf-8'))
    hardware = json.loads((ROOT / 'hardware.json').read_text(encoding='utf-8-sig'))
    for row in rows:
        assert row['environment']['affinity'] == [machine['selected_logical_cpu']]
        assert all(pool['num_threads'] == 1 for pool in row['environment']['threadpool_info'])
        assert row['ready_instances'] == row['case']['fleet']
        for mode in ('single', 'fleet'):
            if row[mode]:
                assert len(row[mode]['samples']) == 5
                assert all(sample['wall_seconds'] > 0 and sample['updates'] > 0 for sample in row[mode]['samples'])
    summaries = []
    for name in NAMES:
        candidates = [row for row in rows if row['case']['detector'] == name
                      and not row['case'].get('parameters') and row['case'].get('sample_seconds', 15) == 15
                      and row['case'].get('evaluate_stride', 1) == 1
                      and row['case'].get('width', 1) == (2 if name == 'Correlation' else 1)
                      and not row['case'].get('pairwise_ui')]
        hot = [row for row in candidates if row['fleet']]
        history = [row for row in candidates if row['case']['category'] == 'baseline' or row['case']['id'] == 'Trend-curvature']
        if len(history) < 3:
            continue
        limiting = min(hot, key=lambda row: row['fleet']['min_wall_updates_per_second'])
        rate = limiting['fleet']['min_wall_updates_per_second']
        history_limiting = min(history, key=lambda row: row['single']['median_wall_updates_per_second'])
        history_rate = history_limiting['single']['median_wall_updates_per_second']
        interval_1 = rate
        if name == 'RuntimeTrend':
            second_cases = [row for row in rows if row['case']['detector'] == name
                            and row['case'].get('sample_seconds') == 1 and row['fleet']]
            interval_1 = min((row['fleet']['min_wall_updates_per_second'] for row in second_cases), default=None)
        multiplier = 2 if name == 'Correlation' else 1
        memory = max(row['state_bytes_per_instance_after'] for row in hot)
        large = by_id.get(name + '-fleet10000')
        passes = ([sample['wall_seconds'] / sample['rounds'] for sample in large['fleet']['samples']] if large else [])
        summary = {
            'detector': name, 'unit': 'pair vectors/s (2 source series)' if name == 'Correlation' else 'samples/s',
            'runtime_conservative_updates_per_second': rate,
            'runtime_cpu_us_per_update': max(row['fleet']['aggregate_cpu_us_per_update'] for row in hot),
            'runtime_limiting_case': limiting['case']['id'],
            'history_updates_per_second': history_rate, 'history_limiting_case': history_limiting['case']['id'],
            'history_1m_points_seconds': 1_000_000 / history_rate,
            'history_speedup_at_15s': 15 * history_rate,
            'runtime_1s_series_70pct': multiplier * math.floor(RESERVE * interval_1) if interval_1 else None,
            'runtime_15s_series_100pct': multiplier * math.floor(15 * rate),
            'runtime_15s_series_70pct': multiplier * math.floor(RESERVE * 15 * rate),
            'runtime_60s_series_70pct': (multiplier * math.floor(RESERVE * 60 * rate) if name != 'RuntimeTrend' else None),
            'state_kib_per_instance': memory / 1024,
            'estimated_state_gib_at_15s_70pct': math.floor(RESERVE * 15 * rate) * memory / 1024**3,
            'measured_10000_pass_median_seconds': statistics.median(passes) if passes else None,
            'measured_10000_pass_max_seconds': max(passes) if passes else None,
        }
        summaries.append(summary)
    csv_file('summary.csv', summaries)
    details = []
    for row in rows:
        case = row['case']
        details.append({'case': case['id'], 'detector': case['detector'], 'scenario': case['scenario'],
            'category': case['category'], 'fleet_instances': case['fleet'], 'width': case.get('width', 1),
            'sample_seconds': case.get('sample_seconds', 15), 'evaluate_stride': case.get('evaluate_stride', 1),
            'parameters': json.dumps(row['parameters'], sort_keys=True),
            'single_median_updates_s': row['single']['median_wall_updates_per_second'],
            'fleet_median_updates_s': row['fleet']['median_wall_updates_per_second'] if row['fleet'] else None,
            'fleet_min_updates_s': row['fleet']['min_wall_updates_per_second'] if row['fleet'] else None,
            'single_cpu_wall_ratio': row['single']['aggregate_cpu_wall_ratio'],
            'fleet_cpu_wall_ratio': row['fleet']['aggregate_cpu_wall_ratio'] if row['fleet'] else None,
            'latency_p50_us': row['wall_latency_us']['p50'], 'latency_p95_us': row['wall_latency_us']['p95'],
            'latency_p99_us': row['wall_latency_us']['p99'],
            'state_kib': row['state_bytes_per_instance_after'] / 1024,
            'ready_points': row['cold']['first_ready']['points'] if row['cold'] and row['cold']['first_ready'] else None})
    csv_file('all_cases.csv', details)
    payload = json.loads(gzip.decompress((ROOT / 'influx-input.json.gz').read_bytes()))
    counts = workload_counts(payload)
    (ROOT / 'workload-counts.json').write_text(json.dumps(counts, indent=2), encoding='utf-8')
    modeled_replay = [[name, number(counts['updates_by_detector'][name]),
                       number(counts['updates_by_detector'][name] /
                              by_id[name + '-influx']['single']['median_wall_updates_per_second'], 1)] for name in NAMES]
    replay_model_table = table(['Детектор', 'Обновлений в текущем полном replay',
                               'Порядковая модель вычислительного времени, с'], modeled_replay)
    main_table = table(['Детектор', 'Runtime, обновлений/с', 'ВР при 1 с, 70%', 'ВР при 15 с, 70%',
                        'ВР при 15 с, 100%', 'Догон истории, обновлений/с', '1 млн точек, с'],
                       [[row['detector'] + (' †' if row['detector'] == 'Correlation' else ''),
                         number(row['runtime_conservative_updates_per_second']), number(row['runtime_1s_series_70pct']),
                         number(row['runtime_15s_series_70pct']), number(row['runtime_15s_series_100pct']),
                         number(row['history_updates_per_second']), number(row['history_1m_points_seconds'], 1)] for row in summaries])
    measured_table = table(['Детектор', '10 000 состояний: проход, медиана / max, с', 'Стейт, KiB/экз.',
                            'RAM состояний при оценке 15 с / 70%, GiB'],
                          [[row['detector'], number(row['measured_10000_pass_median_seconds'], 3) + ' / ' +
                            number(row['measured_10000_pass_max_seconds'], 3), number(row['state_kib_per_instance'], 2),
                            number(row['estimated_state_gib_at_15s_70pct'], 2)] for row in summaries])
    trend_stress = by_id.get('Trend-curvature-fleet10000')
    trend_pass = (statistics.median(s['wall_seconds'] / s['rounds'] for s in trend_stress['fleet']['samples'])
                  if trend_stress else None)
    rt_second = by_id.get('RuntimeTrend-1s-fleet1000')
    rt_pass = (statistics.median(s['wall_seconds'] / s['rounds'] for s in rt_second['fleet']['samples'])
               if rt_second else None)
    sensitivity = []
    for row in rows:
        case = row['case']
        if case['category'] == 'sensitivity':
            base = by_id[case['detector'] + '-events']['single']['median_wall_updates_per_second']
            rate = row['single']['median_wall_updates_per_second']
            setting = json.dumps(case.get('parameters', {}), ensure_ascii=False)
            if case.get('sample_seconds'):
                setting += f'; scrape={case["sample_seconds"]}s'
            if case.get('evaluate_stride'):
                setting += f'; evaluate раз в {case["evaluate_stride"]} точки'
            if case.get('width', 1) > 2:
                setting += f'; {case["width"]} рядов' + ('; как UI, отдельные пары' if case.get('pairwise_ui') else '; один многомерный экземпляр')
            sensitivity.append([case['id'], setting, number(rate), number(rate / base, 2) + '×'])
    sensitivity_table = table(['Сценарий', 'Переопределённые параметры', 'История, обновлений/с', 'К default/events'], sensitivity)
    correlation_rows = []
    for identifier in ('Correlation-events', 'Correlation-group4', 'Correlation-UIgroup4', 'Correlation-group8', 'Correlation-UIgroup8'):
        if identifier not in by_id:
            continue
        row = by_id[identifier]
        width = row['case'].get('width', 2)
        rate = row['single']['median_wall_updates_per_second']
        correlation_rows.append([identifier, width, width * (width - 1) // 2,
                                 number(rate, 1), number(width * math.floor(RESERVE * 15 * rate))])
    correlation_table = table(['Конфигурация', 'ВР в группе', 'Пар в группе', 'Групповых отсчётов/с',
                               'ВР при 15 с / 70%, отдельные группы'], correlation_rows)
    settings_table = table(['Детектор', 'Параметры default', 'Ready-состояние после точек'],
        [[name, json.dumps(by_id[name + '-events']['parameters'], ensure_ascii=False),
          by_id[name + '-events']['cold']['first_ready']['points']] for name in NAMES if name + '-events' in by_id])
    corr_summary = next((row for row in summaries if row['detector'] == 'Correlation'), None)
    all_to_all = math.floor((1 + math.sqrt(1 + 8 * RESERVE * 15 * corr_summary['runtime_conservative_updates_per_second'])) / 2) if corr_summary else None
    total_samples = sum(sum(sample['updates'] for sample in row[mode]['samples'])
                        for row in rows for mode in ('single', 'fleet') if row[mode])
    total_wall = sum(sum(sample['wall_seconds'] for sample in row[mode]['samples'])
                     for row in rows for mode in ('single', 'fleet') if row[mode])
    topology = machine['selected_physical_core_logicals']
    cpu_name = hardware['processor']['Name'].strip()
    warning = '' if len(rows) >= 63 else f'**ПРЕДВАРИТЕЛЬНЫЙ ОТЧЁТ: завершено {len(rows)} из 63 запланированных конфигураций.**\n\n'
    report = f'''# Одноядерный бенчмарк детекторов Atropos

Дата: 12 ноября 2026 года, Europe/Moscow.\n
{warning}Измерены существующие реализации без изменения алгоритмов. Основная единица —
новая точка одного ряда; для Correlation — один синхронный вектор пары из двух рядов.
«Догон истории» означает последовательную обработку накопленных точек с сохранением
состояния, без ожидания следующего scrape. Таблица не оценивает качество обнаружения.
Охвачены все восемь реализаций, подключённых к текущему UI.
Неограниченный поток здесь — повторные `update` / `evaluate` с ограниченным
состоянием окна и без сохранения всех прошлых ответов. Batch-методы и UI `unary`
накапливают списки результатов: бесконечный итератор не даст им конечного ответа
и будет расходовать растущую память.

## Основная таблица

{main_table}

Здесь 1 с / 15 с — интервал поступления **одной новой точки на каждый ряд**.
100% — расчётный предел только по CPU, 70% — та же оценка с 30% запасом.
Неограниченный источник обрабатывается с конечной скоростью столбца «Догон истории».
Если в каждом ряду рождается одна точка за 15 с, скорость догона реального времени
равна скорости этого столбца × 15. Время на миллион точек также вычислено из измеренной скорости;
сам бесконечный ряд и отдельный прогон миллиона точек не заявляются измеренными.

† Correlation: значения ВР относятся к **непересекающимся независимым парам**:
число ВР = 2 × число пар. Скорость в обновлениях/с и миллион точек здесь означают
парные векторы, а не одиночные числа. Для групп см. отдельную таблицу ниже.

RuntimeTrend использует окно в секундах. При scrape 1 с в окне 120 с находятся
121 точка, при scrape 15 с — 9. Поэтому его столбец «1 с» основан на отдельном
замере, а не на линейном пересчёте скорости для 15 с. При scrape 60 с default-окно
120 с содержит всего три точки, меньше min_samples=8: детектор не прогреется;
высокую скорость такого пустого пути нельзя считать рабочей производительностью.

## Что измерено, а что экстраполировано

- Замерено {len(rows)} конфигураций, по пять повторов каждого горячего режима;
  в сохранённых throughput-интервалах обработано {number(total_samples)} обновлений за {number(total_wall, 1)} с wall-time.
  Прогрев, калибровка, cold/latency/profile и подготовка входа в это время не входят.
- Сначала один экземпляр непрерывно догоняет историю, затем 64 / 256 независимых
  экземпляров получают по новой точке круговыми проходами. Дополнительный тест
  проверяет 10 000 независимо размещённых состояний (для корреляции — 10 000 пар).
- Runtime-скорость в основной таблице — минимум измеренных batch-скоростей среди
  подходящих default-сценариев и размеров пула. Для Trend включена дополнительно
  тяжёлая гладкая кривизна с повторным поиском излома. Историческая скорость —
  минимум медиан одиночного потока по quiet, events, Influx и стрессу Trend.
- Числа ВР сверх непосредственно проверенного пула — **CPU-экстраполяция**,
  а не нагрузочный SLA на таком количестве объектов. Память, cache misses,
  очереди, GC, нерегулярность поступления, Influx/PostgreSQL, сеть и графики могут
  ограничить сервис раньше. Запас 30% — инженерное допущение, не доказанная гарантия.
- При смеси конфигураций на одном ядре используется сумма затрат:
  `U = sum(N_i * t_i / scrape_i)`, где `t_i` — секунд CPU на обновление.
  Для рекомендованной оценки целимся в `U <= 0.7`. При одном виде детектора:
  `N_100 = floor(Q_runtime * scrape)`, `N_70 = floor(0.7 * Q_runtime * scrape)`.
  При одновременном поступлении новых точек со скоростью `lambda` остаточный
  догон backlog равен примерно `Q - lambda`; очередь из `B` точек очищается за
  `B / (Q - lambda)`, если `Q > lambda`. Табличный догон — вся мощность ядра,
  без параллельной нагрузки от других рядов и других алгоритмов.

## Прямой проход по большому пулу и память

{measured_table}

Этот прямой проход относится к events/default. У Trend дополнительный тяжёлый
сценарий гладкой кривизны дал **{number(trend_pass, 3)} с на 10 000 состояний**;
основная runtime-оценка учитывает и этот случай, а не только быстрый обычный путь.
Для RuntimeTrend при scrape 1 с отдельно измерены 1000 состояний:
**{number(rt_pass, 3)} с на проход**, поэтому такой пул не укладывается в секундный период.

Проход — **ровно одна новая точка на каждый из 10 000 экземпляров**, время
нормировано по числу полных проходов в батче. Для корреляции это 10 000
парных отсчётов / 20 000 скалярных исходных значений. Это прямой замер,
а не вычисление из одиночной скорости.

Для большого пула подготовлены 16 настоящих прогретых историй. Их состояния
скопированы независимо, включая deques, dataclass, NumPy backing storage и
float-полезную нагрузку. Неизменяемые float-объекты не разделяются между копиями.
Для NumPy-view сохраняется также полный исходный backing array и strides;
обычный deepcopy мог бы уменьшить удерживаемую память eigenvector-матрицы SSA.
Для каждого шаблона восемь будущих score сравнены с отдельным повторным проигрыванием
исходной истории. Новые timed-значения создаются как свежие float; их выдача и
диспетчеризация входят во время. Предобработка шаблонов и копирование не входят.

Стейт — достижимый Python/NumPy-граф состояния, с дедупликацией общих ссылок;
logger и код исключены. Это оценка payload, **не RSS на метрику**: нет фрагментации
аллокатора, общего Python/NumPy/SciPy, сетевых буферов и хранения результатов.
Экстраполяция RAM не заменяет проверку RSS на целевой машине.

## Чувствительность к настройкам

{sensitivity_table}

Все значения — измеренный одиночный догон горячего состояния. Столбец «к default»
сравнивает с events/default того же алгоритма; для групп Correlation единица
меняется на групповой отсчёт, поэтому отношение не является ускорением одного числа.
Замороженная модель SSA и более редкий evaluate RuntimeTrend меняют семантику
или частоту решений; это не бесплатное ускорение эквивалентного алгоритма.

## Корреляция: почему нельзя назвать одно число метрик

{correlation_table}

Для `m` рядов в одной группе вычисляются `P=m*(m-1)/2` пар на каждом временном шаге.
UI сейчас создаёт отдельный детектор на каждую пару. При большой кардинальности
разница между «N независимых пар» и «все N метрик друг с другом» принципиальна.
Из консервативной парной runtime-скорости получаем ориентир **{number(all_to_all)} рядов**
в одной группе «каждый с каждым» при scrape 15 с и 70% CPU. Это решение
квадратичной формулы по парной скорости, **не прямой тест группы такой ширины**;
прямо проверены группы 2, 4 и 8.

## Что делает медленным текущий UI

Read-only snapshot InfluxDB содержит {len(payload['metrics'])} measurement/field,
{sum(len(metric['series']) for metric in payload['metrics'].values())} рядов и {number(counts['source_points'])} исходных точек.
В текущих привязках это даёт {number(counts['correlation_pairs'])} корреляционных пар и
**{number(counts['correlation_vector_updates'])} парных обновлений**, плюс отдельные
проходы однопоточных детекторов. Полные количества сохранены в `workload-counts.json`.

{replay_model_table}

Последний столбец — **вычисленная порядковая модель**, число обновлений / скорость
одиночного Influx-бенчмарка. Это не отдельный замер полного UI и не сумма реальных
latency всех его шагов: разные исходные ряды дают разную цену, cold-прогрев дешевле
горячего потока, а чтение, timestamp-join, формирование результатов и запись добавляют
свои расходы. Модель нужна, чтобы увидеть вклад числа точек и числа пар.

`ui/src/atropos_ui/detector_runtime.py::replay` заново читает историю и создаёт
экземпляры детекторов. `unary` проигрывает все точки, а `correlation` для каждой
пары строит пересечение timestamp и отдельный экземпляр. Фильтр host и легенда
меняют отображение, но не сокращают выполненный replay. Поэтому время открытия /
пересчёта всего набора нельзя сравнивать с временем одного `update(new_point)`.

Чтобы получить табличную runtime-ёмкость в сервисе, состояния должны жить между
циклами и получать только новые timestamps. Для первого старта / смены параметров
нужен отдельный бюджет backfill. Повторный replay всей истории на каждом цикле
оплачивает эту стоимость снова. Этот отчёт не меняет текущий runtime-код.

## Профили и направления оптимизации

Профили сняты отдельно от throughput; их накладные расходы не включены в таблицу.
Проценты в `results/profile-*.txt` относятся к профилированному, более медленному запуску.

- **CUSUM:** `noise_std -> statistics.pstdev -> _ss`, включая точные отношения
  float, Fraction/gcd. Возможная оптимизация — устойчивые скользящие моменты и
  контрольный пересчёт, с проверкой эквивалентности score и quarantine-логики.
- **PredictionInterval:** `_base_prediction` считает mean/stdev дважды за точку:
  для прогнозного интервала и движения следующего. Возможны общие моменты окна,
  но нужно сохранить численную устойчивость и причинность.
- **ACF:** `_profile` проходит окно для каждого лага. Стоимость растёт примерно
  как `window * max_lag`. Уменьшение диапазона лагов сильно помогает, но меняет
  диапазон обнаруживаемых зависимостей; альтернативы с FFT требуют отдельного сравнения.
- **Correlation:** на каждом шаге по каждой паре два CCF-профиля; `_center`,
  `_profile`, относительная ошибка и адаптация создают много маленьких массивов.
  Общая нормализация по ряду/лагу и пакетный расчёт пар могут убрать повторения.
  Рост числа пар остаётся квадратичным даже после локальной оптимизации.
- **SSA:** копирование текущего буфера в NumPy, `_evaluate` / проекция окна и
  `_fit_reference`; refit каждые 16 точек по умолчанию. Цена зависит также от
  автоматически выбранного embedding lag и rank. Нужны профили именно своих рядов.
- **RuntimeTrend:** `_fit`, повторные `_line`, median/sort и robust IRLS на каждом
  evaluate. Вызов evaluate раз в минуту при ingest раз в 15 с измерен отдельно.
- **Trend:** обычный путь поддерживает моменты O(1) амортизированно. Поиск излома
  `_breakpoint` проходит буфер; гладкая кривизна специально проверена как тяжёлый
  сценарий повторных поисков. Один только quiet-бенчмарк недооценивает эту ветку.
- **MAD:** два прохода mean absolute deviation по окнам; это **mean**, а не median
  absolute deviation. Переход на другой статистический смысл не будет эквивалентной оптимизацией.

Для распределения независимых CPU-bound состояний по нескольким ядрам практичен
пул постоянных процессов с закреплёнными шардами рядов. CPython 3.12 ограничивает
одновременное выполнение Python-кода GIL; нативные библиотеки могут освобождать его,
поэтому нельзя одинаково обобщать это на всю NumPy-нагрузку.
[Документация Python 3.12](https://docs.python.org/3.12/library/threading.html).

## Стенд, границы и воспроизведение

- CPU: **{cpu_name}**, {hardware['processor']['NumberOfCores']} физических ядер /
  {hardware['processor']['NumberOfLogicalProcessors']} логических CPU, Windows 11,
  {machine['total_memory']/1024**3:.2f} GiB доступной ОС физической памяти.
- Каждый вычислительный дочерний процесс: affinity **[{machine['selected_logical_cpu']}]**;
  физическое ядро имеет SMT-соседей {topology}. Это один hardware thread одного ядра,
  а не два SMT-потока и не CPU-quota, распределяемая по разным ядрам.
- Другие приложения не останавливались; SMT-сосед не был эксклюзивно зарезервирован,
  Turbo/частота/температура не фиксировались. Результат относится к этой рабочей
  Windows-машине; для Linux / сервера / другой частоты нужно повторить замер.
- CPython {platform_version(rows)}, NumPy {rows[0]['environment']['numpy']}, SciPy {rows[0]['environment']['scipy']}.
  До импорта выставлены OPENBLAS/OMP/MKL/BLIS thread limits=1, затем
  `threadpool_limits(1)`; реальная конфигурация обеих BLAS проверена в каждом JSON.
  [Документация threadpoolctl](https://github.com/joblib/threadpoolctl).
- Служебные OS-потоки библиотек могут существовать, но affinity процесса и
  ограничение BLAS не дают им выполнять вычисления одновременно на разных CPU.
- GC включён. Wall измерен `perf_counter_ns`, CPU — `process_time_ns`.
  Короткая калибровка удлиняется минимум до 0,1 с: Windows CPU-clock может
  квантоваться и возвращать ноль для очень короткого probe. Сохранены оба времени
  и CPU/wall; per-call p95/p99 в `all_cases.csv` — дополнительная выборка 256 вызовов,
  а не SLA очереди на десятках тысяч рядов.
- Quiet: непрерывно меняющийся шум с периодической компонентой; events: шум,
  тренды/ступени/выбросы/смены связи; curvature: гладкая низкошумная кривизна.
  Для Influx взяты Cassandra, screenshot steps, service response и Kafka write;
  в пуле чередуются четыре реальные истории. Для пары — два Cassandra-ряда.
- Вход готовится до таймера. Блоки 4096 значений циклически переиспользуются,
  индексы и timestamps продолжают расти. Такой поток проверяет установившуюся
  цену, а не доказывает качество детекторов на бесконечном неизвестном сигнале.
  В Influx-сценарии сохранены исходные значения, а время подачи RuntimeTrend
  задаётся испытываемым scrape (15 с или 1 с); неравномерность исходного времени
  в этот контролируемый CPU-тест не переносится.
- Хеши исходников — `source-manifest.json`, исходники зафиксированы в `snapshot/apps`.
  Хеши read-only Influx snapshot — `influx-manifest.json`; токены и пароли не сохраняются.

Параметры default / готовность:

{settings_table}

`ready` PredictionInterval после 30 точек означает готовый прогноз для **следующей**,
31-й точки. Correlation ready с пяти точек ещё provisional до полного текущего окна;
в основной таблице измерены прогретые состояния, а не эти быстрые стартовые возвраты.

Из каталога отчёта, PowerShell:

```powershell
$env:PYTHONUTF8 = '1'
uv sync --locked --python 'C:\\Program Files\\Python312\\python.exe'
uv run --locked python benchmark.py --resume
uv run --locked python large_fleet.py
uv run --locked python build_report.py
```

`--resume` пропускает готовые JSON. Для полного нового замера используйте копию
каталога без `results/*.json` и `machine.json`; код выберет малозагруженный логический CPU
или явно задайте `benchmark.py --cpu N`. Запускайте `benchmark.py` и `large_fleet.py`
**последовательно**, чтобы два эксперимента не конкурировали за закреплённый CPU.
Для первого получения / обновления реальных данных используйте `export_inputs.py`
из окружения UI Atropos с настроенным INFLUXDB_TOKEN; обычный benchmark работает
по локальному snapshot и в InfluxDB не обращается.
Для сопоставимости повторов сохраняйте также версию Python 3.12.0; смена версии
интерпретатора может менять стоимость Python-циклов и модуля statistics.

Артефакты: `summary.csv`, `all_cases.csv`, `workload-counts.json`, `results/*.json`,
`results/profile-*.txt`, `machine.json`, `hardware.json`, `source-manifest.json`.
'''
    (ROOT / 'report.md').write_text(report, encoding='utf-8')
    # Minimal standalone HTML: accessible tables, no CDN or executable code.
    def inline(text):
        text = html.escape(text)
        text = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', r'<a href="\2">\1</a>', text)
        text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
        return re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)

    blocks, current_table, code, paragraph = [], [], False, []

    def flush_paragraph():
        if paragraph:
            blocks.append('<p>' + inline(' '.join(paragraph)) + '</p>')
            paragraph.clear()

    for line in report.splitlines():
        if line.startswith('```'):
            flush_paragraph()
            if code:
                blocks.append('</code></pre>')
            else:
                blocks.append('<pre><code>')
            code = not code
            continue
        if code:
            blocks.append(html.escape(line) + '\n')
            continue
        if line.startswith('|'):
            flush_paragraph()
            if line.startswith('| ---'):
                continue
            current_table.append([cell.strip() for cell in line.strip('|').split('|')])
            continue
        if current_table:
            blocks.append('<div class="table"><table><thead><tr>' + ''.join('<th>' + inline(v) + '</th>' for v in current_table[0]) +
                          '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + inline(v) + '</td>' for v in r) + '</tr>' for r in current_table[1:]) + '</tbody></table></div>')
            current_table = []
        if line.startswith('# '):
            flush_paragraph()
            blocks.append('<h1>' + inline(line[2:]) + '</h1>')
        elif line.startswith('## '):
            flush_paragraph()
            blocks.append('<h2>' + inline(line[3:]) + '</h2>')
        elif line:
            if line.startswith('- '):
                flush_paragraph()
            paragraph.append(line)
        else:
            flush_paragraph()
    flush_paragraph()
    document = '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Atropos: одно ядро</title><style>body{font:16px/1.6 system-ui;background:#f7f9fc;color:#182339;max-width:1400px;margin:40px auto;padding:0 28px}h1,h2{line-height:1.25}h2{margin-top:48px}table{border-collapse:collapse;background:white;font-size:14px;width:100%}td,th{padding:11px;border:1px solid #d6deeb;text-align:left}th{background:#182339;color:white}.table{overflow:auto}pre{background:#e8eef7;padding:20px;overflow:auto}p{margin:.5em 0}</style><body>' + '\n'.join(blocks) + '</body></html>'
    (ROOT / 'report.html').write_text(document, encoding='utf-8')
    (ROOT / 'summary.json').write_text(json.dumps({'summaries': summaries, 'case_count': len(rows),
        'measured_throughput_updates': total_samples, 'measured_throughput_wall_seconds': total_wall,
        'workload_counts': counts}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(main_table)
    print(f'Cases={len(rows)}; timed updates={total_samples}; timed wall seconds={total_wall:.1f}')


def platform_version(rows):
    return rows[0]['environment']['python'].split()[0]


if __name__ == '__main__':
    main()
