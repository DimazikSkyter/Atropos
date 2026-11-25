# Experiment3: callback → timeout → retry → queue → GC → OOM

Сервис 1 — Java 21 с Parallel GC, сервис 2 — Go с ограничением работы, нагрузка — k6.
Используется общий мониторинг из ../docker-compose-monitoring.yml.

## Сценарий: OOM в пределах 10 минут

1. 0–5 min: постоянные 750 HTTP RPS, сервис 2 работает 30–40 ms. Сервис 1 устойчиво обрабатывает поток, очередь около нуля.
2. После 5 min: сервис 2 переключается на 40–60 ms. Deadline клиента сервиса 1 — 50 ms, включая отправку и ожидание HTTP callback.
3. После неудачи до двух ретраев через 1 и 2 секунды от конца предыдущей попытки. Ретраи конкурируют с новыми запросами за 48 слотов.
4. Ожидающие отправки запросы остаются в неограниченной DelayQueue и удерживают уникальные payload по 4096 bytes. Объекты переходят в Old generation, растёт работа GC, затем наступает настоящий OOM.
5. При подтверждённом OOM runner останавливает k6 и сохраняет результаты. Десять минут — максимальное время ожидания OOM.

k6 → service1 /submit → DelayQueue → client pool → service2 /work → HTTP 202 → delay → service1 /callback.

Замедляется сервис 2. У сервиса 1 растёт ожидание в клиенте и очереди. Таймаут не включает очередь и не отменяет работу сервиса 2. Каждая попытка имеет отдельный ID: поздний callback не завершает другую попытку. Нет дедупликации в сервисе 2. Он держит максимум 128 работ, при заполнении возвращает 503 и не сохраняет очередь.

## Запуск из корня Atropos

```powershell
docker compose -f infra/docker-compose-monitoring.yml up -d
python infra/experiment3/run.py run
```

Также доступны make experiment3-up, make experiment3-run, make experiment3-down.

```powershell
python infra/experiment3/run.py up    # без нагрузки, деградация ещё не запущена
python infra/experiment3/run.py down  # общий мониторинг и результаты сохраняются
```

Runner пересоздаёт только контейнеры experiment3. Результаты в results/<UTC timestamp>/: verification.json, состояния Docker, логи, k6 summary, сырые метрики, GC log и временные ряды VictoriaMetrics.
Проверяются OOM после baseline и до 600 s, выживание сервиса 2, рост очереди/ретраев, minor и major GC, стабильный baseline, отсутствие dropped_iterations и постоянный поток 750 RPS до OOM. Первый 60-секундный прогрев JVM исключается из оценки доли baseline таймаутов, но входит в полную запись.

Сервис 1: heap -Xms64m -Xmx128m, контейнер 256 MiB, swap отключён. При java.lang.OutOfMemoryError JVM завершается через -XX:+ExitOnOutOfMemoryError. JVM heap OOM может иметь Docker OOMKilled=false; runner подтверждает сообщение JVM и ненулевой exit code. Если срабатывает лимит контейнера, проверяется Docker OOMKilled=true. Нет искусственного exit или отдельной утечки: живые запросы в очереди заполняют heap. Сервис 2 ограничен 128 MiB.

## Grafana и GC

[Один дашборд, 16 панелей](http://localhost:3001/d/experiment3-retry-oom).
Метрики: localhost:18281/metrics и localhost:18282/metrics, scrape каждые 2 s. k6 пишет в VictoriaMetrics через /api/v1/write.

Java Parallel GC: Pause Young = minor (PS Scavenge), Pause Full = major (PS MarkSweep). Панели показывают частоту, накопленное число сборок, суммарное время stop-the-world пауз за секунду и размеры Eden / Survivor / Old. JVM экспортирует MXBean counters. Для графиков GC сервис 2 читает общий GC log: последняя Full GC видна и после OOM сервиса 1. Используется один источник exp3_gc_* без двойного счёта MXBeans.

Стабильная фаза стартует из k6 setup через /start сервиса 2. Runner очищает только текущий GC log при новом запуске; история каждого прогона сохраняется отдельно.

Параметры через переменные окружения PowerShell ($env:...): LOAD_RPS=750, CLIENT_SLOTS=48, PAYLOAD_BYTES=4096, BASELINE_SECONDS=300. Не повышайте CLIENT_SLOTS и не снижайте RPS без проверки: если пул выдерживает поток с ретраями, очередь не растёт. Время OOM зависит от CPU/Docker и проверяется прогоном. Дашборд подписан для стандартных 750 RPS.

При установке в работающий мониторинг примените обновлённый scrape config:

```powershell
Invoke-WebRequest -Method Post http://localhost:8428/-/reload
```

Grafana подхватывает ../grafana/dashboards/experiment3-retry-oom.json.

Расчёт без сетевых накладных расходов: baseline 48/0.035≈1371 attempts/s, degraded 48/0.0475≈1010 attempts/s. При ~50% таймаутов потребность 750×1.75≈1313 attempts/s превышает capacity. Фактические RPS сервиса 2 ограничены пулом: ретраи вытесняют новые отправки. Нагрузка остаётся постоянной, переход вызван изменением обработки и повторными попытками.

k6 использует [constant-arrival-rate](https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/constant-arrival-rate/). dropped_iterations проверяется отдельно, нехватка VU не считается успешным экспериментом.

Независимая повторная проверка результатов:

python infra/experiment3/verify.py infra/experiment3/results/<UTC timestamp>

DNS k6 кешируется на время теста. После OOM нагрузка прекращается, поэтому сбои сети к уже остановленному контейнеру не входят в эксперимент. Общий мониторинг остаётся работать.

