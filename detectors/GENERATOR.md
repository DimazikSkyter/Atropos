# Генератор метрик и аномалий

Все команды выполняются из каталога `Atropos/detectors`. Зависимости и их lock
управляются UV: NumPy нужен генератору, Matplotlib входит в группу `dev` для графиков.

```powershell
uv sync --locked
uv run python -m unittest tests.test_generator -v
uv run python -m tests.visualize_generator --output artifacts/generator
```

В репозитории используется каталог `apps`, поэтому пакет находится в
`detectors/apps/generator`. Он возвращает `apps.metrics.MetricResult` с полями
`name`, `tags`, `values: list[float | int]`, `timestamps: list[int]`. Сохранён
конструктор из приложенного `Metrics.py`, включая входной JSON матрицы Prometheus.
Timestamps — Unix seconds; значения конечны, timestamps строго возрастают.

```python
from datetime import datetime, timedelta, timezone

from apps.generator import (
    AnomalyGenerator, CycleConfig, NoiseConfig, SpikeConfig,
    TimeSeriesGenerator, configure_json_logging,
)

configure_json_logging()  # JSON lines в stderr; root logger не меняется.
generator = TimeSeriesGenerator(
    name='request_rate', tags={'host': 'emulator'}, mean=4300,
    noise=NoiseConfig(
        white_std_percent=0.35, correlated_std_percent=0.4,
        correlation=0.65, heavy_tail_std_percent=0.1, degrees_of_freedom=5,
    ),
    spikes=SpikeConfig(probability=0.001, max_amplitude_percent=8, max_points=3),
    # Если start_time не указан, начало — 00:00:00 текущего локального дня.
    start_time=datetime(2026, 10, 4, tzinfo=timezone(timedelta(hours=3))),
    step_seconds=15, seed=42,
)
original = generator.generate(960)
anomalous = AnomalyGenerator(
    original, start=400, stop=430, anomaly=lambda value: value * 1.5,
).generate()
```

Время задаётся **только конструктору** `TimeSeriesGenerator`; `generate(points)`
принимает число точек. `start_time` допускает datetime или целые Unix seconds.
Naive datetime и начало дня используют локальную таймзону процесса. Для явной
таймзоны передайте aware datetime. Шаг — целое положительное число секунд,
по умолчанию 15. Начало должно иметь точность до целой секунды.

Оригинал сохраняется: `AnomalyGenerator` снимет копию входных списков и тегов,
вызовет лямбду для каждого оригинального значения в диапазоне **[start, stop)**
и вернёт новый `MetricResult` с теми же timestamps, именем и тегами. Индексы
начинаются с нуля, stop исключается. Повторный `generate()` снова обрабатывает
исходные значения. Для нескольких диапазонов создайте следующий
`AnomalyGenerator` от предыдущего результата. Функция может задать рост, провал,
ступеньку или любое конечное числовое значение; ограничение 50% относится
только к фоновым всплескам базового генератора.

| Параметр | Семантика |
|---|---|
| `mean` | Постоянный ожидаемый уровень, default 100; без детерминированного наклона |
| `NoiseConfig.white_std_percent` | Стандартное отклонение независимого Gaussian шума в % от abs(mean), default 0.35 |
| `correlated_std_percent`, `correlation` | Стандартное отклонение и коэффициент стационарного AR(1), defaults 0.4 и 0.65; -1 < correlation < 1 |
| `heavy_tail_std_percent`, `degrees_of_freedom` | Нормированный Student-t шум; defaults 0.1 и 5, df > 2 |
| `cycles` | От 0 до 3 синусоид, по умолчанию пусто |
| `CycleConfig.period_points` | Период в точках, целое >= 2 |
| `CycleConfig.amplitude_percent` | Пиковая амплитуда в % от abs(mean), не peak-to-peak |
| `CycleConfig.phase_radians` | Начальная фаза, default 0 |
| `SpikeConfig.probability` | Вероятность начала события в свободной точке, default 0.001; 0 отключает |
| `min_amplitude_percent`, `max_amplitude_percent` | Добавка в % от abs(mean), defaults 1–8; 0 <= min <= max <= 50 |
| `max_points` | Длительность фонового события от 1 до заданного максимума (1–3), default 3 |
| `seed` | Локальный NumPy RNG; одинаковые seed/аргументы у новых экземпляров дают одинаковые результаты |

Три шумовых компонента независимы. AR(1) сразу начинается в стационарном
распределении, поэтому отсутствует прогрев или случайное блуждание уровня.
У отдельной реализации среднее и оценка наклона могут случайно отличаться от
ожидаемых. Положительные фоновые всплески слегка повышают выборочное среднее.
Масштабирование на abs(mean) позволяет отрицательные уровни; при mean=0
процентный шум, циклы и фоновые всплески также равны нулю. Значения не обрезаются:
например, достаточно сильный шум может дать отрицательную метрику.

После генерации `generator.background_anomaly_ranges` содержит интервалы
фоновых событий `[start, stop)`. Соседние события разделены хотя бы одной обычной
точкой. Каждый вызов `generate()` создаёт ряд с тем же начальным временем,
продвигая RNG; это генерация отдельных сценариев, а не продолжение потока.

```python
cyclic = TimeSeriesGenerator(
    mean=270, seed=42,
    cycles=(CycleConfig(120, 2), CycleConfig(32, 0.5), CycleConfig(8, 0.2)),
).generate(960)

# Полностью плоская серия:
flat = TimeSeriesGenerator(
    mean=270, noise=NoiseConfig(0, 0, 0, 0), spikes=SpikeConfig(probability=0),
).generate(960)
```

Передача в существующий потоковый детектор:

```python
from apps.runtime_trend import RuntimeTrendDetector, TrendConfig

config = TrendConfig(window_seconds=120, variance_threshold=100, min_tangent=1)
for metric in (original, anomalous):
    detector = RuntimeTrendDetector(config)  # независимое состояние для каждого ряда
    for timestamp, value in zip(metric.timestamps, metric.values):
        detector.update(timestamp, value)
        result = detector.evaluate()
```

Визуализация создаёт `report.png` и `series.json` с оригиналами и вариантами
с аномалиями: базовый шум без циклов, три цикла, короткий пик, ступенька и провал.
Ось Y всех графиков временных рядов начинается с 0.
JSON-логи включают настройки генерации, диапазоны внесения аномалий и ошибки
лямбды. Для логирования в файл передайте `stream` в `configure_json_logging`.
