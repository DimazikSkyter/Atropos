# Каталог VictoriaMetrics

`apps/metrics.py` — read-only клиент на стандартной библиотеке Python.
Зависимости и окружение управляются существующим UV-проектом `detectors`.
Он ничего не записывает и не удаляет в VictoriaMetrics.

Список имён, полученный этим клиентом, можно передать в `apps.services.MetricService`.
Сервис управляет только метаданными и локальными `additional_labels`; клиент API
в него не входит. Примеры: [METRIC_SERVICE.md](METRIC_SERVICE.md).

```python
from apps.metrics import VictoriaMetricsClient

vm = VictoriaMetricsClient('http://localhost:8428')
names = vm.list_metric_names()  # уникальные имена, без всех комбинаций тегов
tags = vm.list_label_names()  # включая __name__
jobs = vm.list_label_values('job')
series = vm.list_series()  # список MetricSeries(name, tags)
catalog = vm.catalog()  # снимок всех комбинаций имени и тегов

windows = catalog.filter(prefix='windows_')
cpu = windows.filter(name_regex='.*cpu.*', labels={'job': 'windows-exporter'})
selected = catalog.filter(names=['up'], exclude_names=['unwanted'])
by_instance = catalog.filter(label_regex={'instance': '.*:9182'})

print(cpu.names, cpu.label_names, cpu.label_values('core'))
for entry in cpu.series[:3]:
  print(entry.name, entry.labels, entry.selector())

# Серверная фильтрация уменьшает объём ответа до локальной фильтрации.
selector = '{job="windows-exporter"}'
activity = vm.recent_activity(selector, lookback_seconds=300)
for item in activity:
  print(item.series.name, item.status, item.has_data, item.has_nonzero)

has_any_nonzero = vm.has_recent_nonzero(selector, lookback_seconds=120)
available = vm.active_catalog(selector, lookback_seconds=120)
```

## Семантика

- Метрика и временной ряд различаются: одно имя может иметь много комбинаций тегов.
  `MetricSeries.tags` — неизменяемый кортеж пар без `__name__`; `labels` возвращает
  новый словарь с `__name__`. `selector()` экранирует значения и формирует точный
  селектор для последующего класса PromQL. Это ещё не конструктор выражений PromQL.
- Каталог — неизменяемый снимок; `filter()` возвращает новый. Для обновления нужно
  повторить `catalog()`. Фильтры объединяются через AND, regex — Python `fullmatch`.
  Отсутствующий тег не совпадает даже с пустой строкой. Для подстроки используйте `.*cpu.*`.
- По умолчанию каталог запрашивает последний час. `lookback_seconds` и `end`
  задают диапазон; `end` — Unix timestamp в секундах, по умолчанию сейчас.
  «Все» означает все имена/ряды, возвращённые сервером для селектора и периода,
  а не все когда-либо существовавшие данные. Для всей сохранённой истории задайте
  lookback, покрывающий retention. Список не ограничивается клиентским `limit`.
- VictoriaMetrics округляет диапазоны metadata API до суток. Поэтому наличие
  записи в каталоге **не доказывает** наличие свежих samples. Проверка активности
  отдельно читает `/api/v1/export` и проверяет реальные timestamps в точном диапазоне.
- `nonzero`: есть хотя бы одно конечное значение с `abs(value) > zero_tolerance`.
  Отрицательные значения тоже учитываются; противоположные значения не взаимоуничтожаются.
- `zero_only`: есть конечные значения, но все в пределах допуска (по умолчанию ровно 0).
- `invalid_only`: есть только null/NaN/Inf. `no_data`: ни одного sample в диапазоне.
  При смеси чисел и нечисловых данных status определяется конечными значениями,
  а `invalid_samples` сохраняет число остальных. Отсутствие данных не приравнивается к 0.
- `last_sample_at` — последний timestamp любого sample, включая нечисловой.
  `has_data` означает наличие хотя бы одного конечного значения.
  `has_recent_nonzero` возвращает bool для всего селектора; для подробностей используйте activity.
- Экспорт обрабатывается построчно; повторные чанки одного ряда объединяются.
  Сырые точки не интерполируются и не пересэмплируются. Счётчики отражают samples,
  возвращённые сервером (его правила дедупликации могут влиять на количество).

Сбой сети/API или повреждённый ответ вызывает `VictoriaMetricsError`, а не пустой
каталог. Предупреждения metadata API передаются через Python warnings. Серверные
ограничения на объём/число рядов сохраняются: универсальной пагинации этих API нет.
Для больших инсталляций используйте серверные selectors по job/имени/instance и
короткие диапазоны активности. Экспорт всех samples миллиона рядов дорог; этот
клиент предназначен для управления каталогом, не для проверки всех рядов каждый scrape.
`catalog()` хранит весь полученный каталог в памяти, activity — агрегат на каждый ряд.
Каждый вызов activity/active_catalog/has_recent_nonzero делает новый запрос; готовый
список activity лучше переиспользовать, если нужны несколько представлений одновременно.

Для кластера передайте URL вида `http://vmselect:8481/select/0/prometheus`.
`timeout` задаёт сетевой timeout, `headers` позволяет передать авторизацию.

## Проверка на запущенной VictoriaMetrics

Из папки `detectors`:

```powershell
uv run python -m unittest tests.test_victoria_metrics -v
$env:RUN_VM_INTEGRATION='1'
$env:VICTORIA_METRICS_URL='http://localhost:8428'
uv run python -m unittest tests.test_victoria_metrics_live -v
Remove-Item Env:RUN_VM_INTEGRATION
```

Живой smoke-тест читает все имена, теги, ряды, фильтрует windows/CPU, проверяет
активность windows-exporter за 120 секунд. `VM_TEST_SELECTOR` меняет этот селектор.
Тест печатает фактическую сводку и проверяет только корректность вызовов и типов:
он **не требует** конкретных метрик, количества рядов или ненулевых значений.
При недоступной VictoriaMetrics явно включённый тест падает. Без флага он пропускается,
чтобы обычные unit-тесты не зависели от инфраструктуры. Данные в VM не записываются.

API: https://docs.victoriametrics.com/victoriametrics/url-examples/
Округление metadata и JSON-line export: https://docs.victoriametrics.com/victoriametrics/
