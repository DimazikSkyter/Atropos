# Experiment4: тренд minor GC и структура времени отклика

Да, такой эксперимент можно построить: рост фоновой аллокации увеличивает частоту minor GC, а stop-the-world паузы воздействуют на запросы в той же JVM. Часть запросов проходит между паузами, часть задерживается. Это может увеличить дисперсию и вес правого хвоста; заметная вторая мода зависит от длительности пауз, времени запроса и планирования потоков. Скрипт проверки отделяет наблюдаемый результат от желаемого эффекта. Наличие второй моды нельзя заранее обещать.

Здесь работает отдельная JVM, без вызовов других сервисов Atropos. k6 поддерживает **постоянные 250 RPS** в открытой модели `constant-arrival-rate`. Во время одного запуска меняется только скорость фоновой аллокации. Обработчик `/work` имеет фиксированную работу, не переключает режимы по времени и не подмешивает искусственный медленный путь.

## Профиль

| Фаза | Длительность | Аллокация |
| --- | ---: | ---: |
| Baseline | 180 с | 16 MiB/s |
| Ramp | 900 с | линейно 16 → 640 MiB/s |
| Hold | 180 с | 640 MiB/s |

Полный запуск — **21 минута**, из которых 15 минут занимает плавное воздействие. После hold allocator возвращается к baseline. Старт задается `POST /start`, который делает k6 после готовности JVM. Повторный `/start` в той же JVM запрещен; runner пересоздает контейнер для нового запуска.

Фоновые массивы живут приблизительно 60 ms. Удерживаемый объем растет вместе со скоростью аллокации, поэтому изменяется стоимость копирования живых объектов при minor GC. JVM: Java 21, `ParallelGC`, один GC worker, фиксированные heap 768 MiB и young generation 192 MiB, `SurvivorRatio=2`, адаптивное изменение размера отключено. Compose ограничивает JVM 4 CPU и 1280 MiB памяти без дополнительного swap. k6 получает отдельно 2 CPU и 512 MiB.

`/work` выполняет checksum с фиксированным бюджетом приблизительно 8 ms **CPU-времени текущего потока**. Во время stop-the-world GC этот бюджет не расходуется: пауза добавляет реальное время к завершению запроса. Работа не зависит от фазы и скорости allocator. При 250 RPS обработчики потребляют примерно 2 CPU, оставляя запас внутри лимита 4 CPU для allocator, GC и HTTP. Отдельная вторая мода вероятнее, если длительности GC пауз достаточно стабильны; при широком разбросе пауз может появиться только хвост.

## Мониторинг

Используется существующий `infra/docker-compose-monitoring.yml`: VictoriaMetrics на порту 8428, Grafana на 3001 и datasource UID `victoria-metrics`. В compose эксперимента нет отдельных Grafana или VictoriaMetrics.

Интеграция состоит из:

- `integration/scrape-fragment.yml`: добавить job `experiment4` в существующий `scrape_configs` файла `infra/victoria-metrics/scrape.yml`. Scrape каждые 2 с, target `host.docker.internal:18381`, label `experiment=experiment4`.
- `dashboard.json`: установить как `infra/grafana/dashboards/experiment4-gc-distribution.json`. Существующий file provider помещает его в папку Atropos.

После изменения scrape конфигурации выполнить POST http://localhost:8428/-/reload. Grafana считывает файл dashboard через существующее provisioning. Все остальные jobs и настройки мониторинга сохраняются.

Из корня репозитория Atropos:

```powershell
docker compose -f infra/docker-compose-monitoring.yml up -d
python infra/experiment4/run.py up
python infra/experiment4/run.py run
```

Открыть [dashboard Experiment4](http://localhost:3001/d/experiment4-gc-distribution). Runner сохраняет абсолютный диапазон времени в `metadata.json`; использовать его для сравнения завершенных опытов. При нестандартном порте VictoriaMetrics задать `VICTORIA_METRICS_PORT` для k6 в Compose и `VICTORIA_METRICS_URL` для runner. Адрес Java runner задается через `EXPERIMENT4_URL`.

Пилот — только проверка запуска и калибровка; он не удовлетворяет требованию длительности основного опыта:

```powershell
python infra/experiment4/run.py run --pilot
```

Контроль с постоянной аллокацией при тех же RPS и длительностях:

```powershell
python infra/experiment4/run.py run --control
```

Перепроверка сохраненного результата и остановка:

```powershell
python infra/experiment4/verify.py infra/experiment4/results/<UTCstamp>
python infra/experiment4/run.py down
```

Для ручного запуска из `infra/experiment4`:

```powershell
docker compose up -d --build java
docker compose --profile load run --rm k6
```

Ручной запуск пишет в `results/current` и не выполняет сбор дополнительных данных runner. Перед новым ручным запуском пересоздать JVM, чтобы `POST /start` не получил 409. Для воспроизводимых результатов предпочтителен runner с отдельным каталогом для каждого запуска.

## Micrometer histogram

Главная метрика — настоящий Micrometer `Timer`, а не сгенерированные значения histogram:

```text
exp4_request_duration_seconds_bucket{service="java",status="200",le="..."}
exp4_request_duration_seconds_count{service="java",status="200"}
exp4_request_duration_seconds_sum{service="java",status="200"}
```

Timer измеряет `/work` от входа в обработчик до завершения записи HTTP-ответа. Время в очереди до вызова обработчика не входит; клиентское end-to-end ВО отдельно сохраняет k6. Запросы мониторинга и управления не входят в histogram `/work`.

SLO boundaries в миллисекундах: `0.5, 1, 2, 4, 5, 6, …, 50, 60, 75, 100, 150, 250, 500, 1000, +Inf`. Buckets в диапазоне возможных мод имеют шаг 1 ms. Источник структуры распределения — именно приращения этих cumulative counters за окно 60 с.

Heatmap использует запрос:

```promql
sum by (le) (
  rate(exp4_request_duration_seconds_bucket{
    experiment="experiment4",service="java",status="200",le!="+Inf"
  }[60s])
)
```

В Prometheus datasource задан **Format = Heatmap**, который преобразует cumulative histogram в непересекающиеся интервалы. В панели отключен **Calculate from data**: Grafana использует готовые buckets и не строит histogram из усредненных latency. Верхняя граница отображения 50 ms увеличивает область возможных мод; buckets до 1 s остаются в VictoriaMetrics. Более медленные ответы учитываются в count, sum и sigma, а overflow показан отдельной панелью.

Sigma считается по индивидуальным наблюдениям через накопленную сумму квадратов `exp4_request_duration_seconds_squared_sum`. В обозначениях `N = rate(count[60s])`, `S = rate(sum[60s])`, `Q = rate(squared_sum[60s])`:

```text
mean  = S / N
sigma = sqrt(max(Q / N - (S / N)^2, 0))
```

Это sigma ВО в текущем окне, а не дисперсия временного ряда средних значений. Сам histogram показывает форму распределения; sigma подтверждает изменение разброса. `histogram_quantile` дает приблизительные p50/p95/p99 с точностью, ограниченной шириной buckets.

Проверить реальный exporter можно без Grafana:

```powershell
(Invoke-WebRequest http://localhost:18381/metrics).Content | Select-String 'exp4_request_duration_seconds_(bucket|count|sum)'
```

## Отчет из Micrometer buckets

`render_report.py` строит статический отчет непосредственно из `vm-export.ndjson` и `metadata.json`. Для него нужны Python с `numpy` и `matplotlib`; runner и `verify.py` используют стандартную библиотеку.

```powershell
python infra/experiment4/render_report.py infra/experiment4/results/<UTCstamp> `
  --output infra/experiment4/results/<UTCstamp>/report.png `
  --summary infra/experiment4/results/<UTCstamp>/report-summary.json
```

По умолчанию `report.png` сохраняется в каталоге запуска; `--output` выбирает другой путь, `--summary` дополнительно сохраняет JSON со статистикой целостности и фаз. Окно — 60 с, шаг — 10 с. Их можно задать через `--window-seconds` и `--step-seconds`.

Отчет показывает heatmap приращений настоящих cumulative Micrometer buckets, minor GC rates, mean/sigma из moment counters и сравнение baseline с последними 120 с hold. Первые `min(60, baseline/3)` секунд исключены из сравнения как прогрев; по умолчанию baseline оценивается на 60–180 с. Цвет heatmap использует gamma=0.35 для видимости малых долей, сохраняя численные requests/s; отображение ограничено 50 ms (`--max-latency-ms`). Соседние cumulative boundaries вычитаются, давая непересекающиеся интервалы. В сравнении фаз вероятность делится на ширину интервала; шкала symlog позволяет видеть buckets с небольшим весом. Серверные или клиентские CSV не используются для построения формы распределения.

Scrape gaps более 6 с отмечаются штриховкой. Окна, пересекающие gap, reset счетчика или несогласованные histogram counters, исключаются; число валидных окон записывается в summary. На границах окон используются линейные интерполяции соседних реальных scrape samples, без экстраполяции и без соединения данных через gap. Пилот имеет только 20 с baseline и 30 с hold, поэтому его heatmap не содержит полного 60 с окна внутри этих фаз; это не потеря scrape данных.

Сырые наблюдения k6 пишутся как gzip NDJSON: `k6.ndjson.gz`. При анализе читать их через `gzip.open(path, "rt", encoding="utf-8")`; статистика k6 также доступна в несжатом `k6-summary.json`.

## Как интерпретировать результат

Положительный результат требует совместного наблюдения:

1. Рост **частоты minor collections/s** и/или времени minor GC в окне по мере ramp; cumulative GC counter сам по себе всегда растет и не доказывает тренд.
2. Изменение распределения ВО в Micrometer buckets: расширение полосы, рост правого хвоста, рост sigma; при наличии второй моды — отдельная полоса с промежутком меньшей плотности.
3. Постоянный offered и completed RPS, отсутствие dropped iterations и HTTP ошибок, отсутствие выраженного CPU throttling и очереди.
4. Индивидуальные замедленные запросы чаще пересекаются с GC. Для overlap используется `requests.csv` и интервалы `Pause Young/Full` из unified JVM GC log. Начала и окончания пауз связываются по GC ID и uptime. У GC notifications собственное начало шкалы времени может отличаться, поэтому `gc-events.csv` независимо сверяется с log по порядку и виду событий; постоянный offset и его разброс выводятся явно.
5. Контроль `--control` не воспроизводит тот же рост структуры при постоянном allocator.

Корреляция minor GC rate и sigma сама по себе не доказывает причинность. Более сильное подтверждение дают фиксированная нагрузка, изменение только allocator, совпадение request/GC интервалов и контрольный опыт.

Runner сохраняет `metadata.json`, snapshots `metrics.ndjson`, серверные `requests.csv`, GC notifications `gc-events.csv`, unified JVM GC log, сырые k6 наблюдения `k6.ndjson.gz`, `k6-summary.json`, `vm-export.ndjson`, состояние Docker и `verification.json`. `gc_intervals.py` проверяет согласованность clocks через wall-clock start metadata и ISO timestamps/uptime unified log; clock offset не подбирается по задержкам запросов. `bucket_analysis.py` вычисляет вероятности интервалов и total variation distance между отдельными baseline/hold окнами прямо из Timer buckets; порог изменения — TV≥0.02. Для GC не используется JFR. Логи пишутся асинхронно; dropped-log counters проверяются отдельно.

На dashboard вынесены major GC, heap/pools, CPU, throttling, queue и потери данных. Если major GC или throttling заметно растут, меняется несколько причин задержки и опыт следует откалибровать. Начинать с пилота; при saturation уменьшить `ALLOC_END_MIB` или увеличить доступный CPU. Если растет old generation, уменьшить retention/end allocation. При повышении аллокации учитывать живой объем `rate × retention`: его превышение емкости survivor способствует promotion и major GC.

Вторая мода — проверяемый результат, не автоматически успешный исход. Если есть только увеличение sigma и хвоста, именно это должно быть записано в выводе опыта. Не менять задержку обработчика во времени ради красивой полосы.

Документация: [Micrometer Timer](https://docs.micrometer.io/micrometer/reference/concepts/timers.html), [Grafana Prometheus Heatmap format](https://grafana.com/docs/grafana/latest/datasources/prometheus/query-editor/), [pre-bucketed heatmaps](https://grafana.com/docs/grafana/latest/fundamentals/intro-histograms/), [k6 constant arrival rate](https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/constant-arrival-rate/), [k6 remote write](https://grafana.com/docs/k6/latest/results-output/real-time/prometheus-remote-write/).
