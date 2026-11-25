# Воспроизведение метрик в InfluxDB

`infra/docker-compose-monitoring.yml` запускает InfluxDB **2.7.12**, однократный
загрузчик истории и готовый дашборд Grafana. База и конфигурация сохраняются в
volumes `monitoring_influxdb_data` и `monitoring_influxdb_config`; срок хранения
bucket — **365 дней (1 год)**. Данные сохраняются после выключения/включения ПК,
перезапуска Docker, обычного `down` и пересоздания контейнера с теми же volumes.
`down --volumes` удаляет эти volumes.

InfluxDB получает минуту на штатную остановку (`stop_grace_period: 1m`). Запись WAL
синхронизируется без задержки (`INFLUXD_STORAGE_WAL_FSYNC_DELAY=0s`), а при остановке
выполняется сброс WAL (`INFLUXD_STORAGE_WAL_FLUSH_ON_SHUTDOWN=true`).

`INFLUXDB_RETENTION=365d` задает retention при первой инициализации. Скрипт
`seed-replays.sh` также обновляет retention **существующего** bucket перед импортом,
поэтому настройка применяется к ранее созданной базе. Возраст считается от timestamp
метрики, а не от момента загрузки. Очистка выполняется фоновым сервисом по группам
шардов; для новых групп задано 24 часа.

При старте Grafana `provision-grafana.sh` объединяет существующее provisioning
с InfluxDB в `/tmp/grafana-provisioning` и запускает стандартный `/run.sh`.
Это обходит запрет Docker Desktop на добавление файлов внутрь read-only bind mount.
Дашборды и база Grafana используют прежние пути; полный `docker-compose.yml`
не изменяется. После изменения файлов datasource/provider перезапустите Grafana.

Из корня Atropos:

```powershell
docker compose -f infra/docker-compose-monitoring.yml up -d
```

Если используется `infra/.env`, передайте `--env-file infra/.env` перед `-f`.
Параметры `INFLUXDB_*` описаны в `infra/.env.example`; одинаковые значения применяются
для инициализации базы, загрузчика и datasource. Изменение этих переменных после
первой инициализации не меняет существующие учетные данные InfluxDB.

- [График в Grafana](http://localhost:3001/d/cassandra-response-time-replay)
  — папка **Atropos**, дашборд **Cassandra response time — replay**.
- [InfluxDB](http://localhost:8086): локальный пользователь `atropos`, пароль
  `atropos-influxdb`, если не изменены в окружении.

## Общий дашборд

[InfluxDB Replay](http://localhost:3001/d/influxdb-replay) содержит отдельную секцию
для каждого графика и ссылки на детальный просмотр с исходным диапазоном времени.
В обзоре выбран общий диапазон всех историй: графики с разными датами занимают разные
участки оси времени. Для полной детализации открывайте ссылку на нужный график.

`build_overview.py` автоматически собирает обзор из `dashboards/*.json`. Чтобы добавить
следующий график, разместите его дашборд в этой папке, а line protocol — в отдельной
подпапке InfluxDB. Загрузчик импортирует все `*.lp` в корне и подпапках.

```powershell
python infra/influxdb/build_overview.py
docker compose -f infra/docker-compose-monitoring.yml run --rm influxdb-seed
```

Grafana считывает изменения дашбордов через polling каждые 30 секунд.

Текущие графики:

- 01–02.10.2026 — [Cassandra](http://localhost:3001/d/cassandra-response-time-replay).
- 03.10.2026 — [request_handle_time / total_rps](http://localhost:3001/d/request-handle-time-replay),
  исходники и параметры в [request-handle/README.md](request-handle/README.md).

## История Cassandra

Исходник — `reference.png`, числовых измерений от Cassandra нет. История представляет
приблизительную оцифровку четырех основных цветных линий скриншота. Скрытые наложением
участки заполнены предыдущими значениями; высокие ступени откалиброваны по сетке.
Для частых колебаний сохранены оба конца вертикальных штрихов, разнесенные на
соседние 15-секундные отсчеты. Точное время этих колебаний внутри пикселя неизвестно.
Дополнительная бледная линия у нуля на скриншоте не включена: в задании четыре ноды.

| Параметр | Значение |
| --- | --- |
| Measurement / имя метрики | `cassandra_response_time` |
| Поле | `value`, миллисекунды |
| Tag / лейбл | `host` |
| Хосты | `cassandra-1` (синий), `cassandra-2` (оранжевый), `cassandra-3` (красный), `cassandra-4` (фиолетовый) |
| Начало | 01.10.2026 12:30:00, Europe/Moscow |
| Конец, включительно | 02.10.2026 12:30:00, Europe/Moscow |
| Интервал отсчетов | 15 секунд |
| Точки | 5 761 на хост; 23 044 всего |

Временные отметки соответствуют часам изображения: оно охватывает сутки с полудня
до следующего полудня. В line protocol время хранится как Unix timestamp в секундах.
Пример:

```text
cassandra_response_time,host=cassandra-1 value=1.000 1790847000
```

Дашборд запрашивает **сырые точки без `aggregateWindow`**. `maxDataPoints=10000`
и интервал `15s` сохраняют детализацию полного диапазона. Шкала 0–40 ms, ступенчатые
линии и цвета соответствуют изображению; в легенде и tooltip отображается `host`.
Переменная `host` позволяет выбирать ноды. Окно времени зафиксировано на истории,
а обновление отключено, чтобы график не переключался на текущую дату.

## Повторная загрузка и перенос на другую дату

```powershell
docker compose -f infra/docker-compose-monitoring.yml run --rm influxdb-seed
```

Повторная запись того же measurement/tag/timestamp заменяет поле точки, поэтому
загрузчик можно безопасно повторять; число точек остается прежним.

`generate.py` использует только стандартную библиотеку Python. Он читает
`reference-traces.csv`, дублирует предыдущую оцифрованную точку до следующей с шагом
15 секунд и обновляет line protocol, CSV, `metadata.json` и диапазон дашборда:

```powershell
python infra/influxdb/generate.py
python infra/influxdb/generate.py --date 2026-10-01
docker compose -f infra/docker-compose-monitoring.yml run --rm influxdb-seed
```

При выборе другой даты старые данные остаются в bucket, а дашборд показывает новый
диапазон. Готовые файлы позволяют запускать стенд без Python и дополнительных пакетов.
`preview.png` показывает восстановленные ряды. `metadata.json` содержит параметры
и счетчики для проверки.

Повторная оцифровка изображения доступна через `digitize_reference.py` (не нужна
для запуска стенда). Для нее нужны `numpy`, `Pillow`, `matplotlib`; после оцифровки
выполните `generate.py`, чтобы обновить файлы импорта.

Настройки основаны на документации [InfluxDB Docker Compose](https://docs.influxdata.com/influxdb/v2/install/use-docker-compose/)
и [Grafana InfluxDB datasource](https://grafana.com/docs/grafana/latest/datasources/influxdb/configure/).
Retention описан в [InfluxDB data retention](https://docs.influxdata.com/influxdb/v2/reference/internals/data-retention/),
параметры WAL — в [InfluxDB configuration](https://docs.influxdata.com/influxdb/v2/reference/config-options/#storage-wal-fsync-delay).


## TPS с привязкой к Cassandra

01–02.10.2026 — [ВО Cassandra / TPS](http://localhost:3001/d/cassandra-tps-correlation).
Семь TPS-серий со скриншота привязаны к событиям существующей истории Cassandra;
подробности, временные отметки и ограничения в [cassandra-tps/README.md](cassandra-tps/README.md).


## ВО клиента и диспетчер сервиса

05.10.2026 — [ВО клиента / диспетчер](http://localhost:3001/d/sync-client-dispatcher-replay).
Верхний график содержит только основные линии 6–24 ms; нижний — задержку
диспетчера в секундах. Начало роста задержки согласовано со скачком уровня ВО.
Исходники и ограничения: [sync-dispatcher/README.md](sync-dispatcher/README.md).


## Ступенчатые ряды со скриншота, 17–18 октября 2026

[Четыре цветных ряда](http://localhost:3001/d/screenshot-steps-20261017):
17.10.2026 14:23:30 — 18.10.2026 06:52:15, Москва; шаг 15 секунд.
Общий график и четыре отдельных панели добавлены в InfluxDB Replay.
Measurement `screenshot_step_replay`, поле `value`, tag `trace`.
Единица отображения — TPS; нижнее оранжевое плато = 100 TPS.
Значения восстановлены по картинке. Общий график называется «Сценарий».
Подробности реконструкции: [step-screenshot/README.md](step-screenshot/README.md).
