# Cassandra compaction-debt lab

Локальный стенд для демонстрации положительной обратной связи в LSM-хранилище:

`writes -> SSTables -> compaction backlog -> read/write amplification -> node loss -> extra load on survivors`.

Это не эмуляция одного старого deadlock-багa. Стенд намеренно создаёт системный режим, в котором входящий поток быстрее compaction. Используется Cassandra 4.1.12, LCS, принудительный flush каждые 3 секунды, один compactor и лимит compaction 1 MiB/s.

Границы модели и ссылки на первичные источники описаны в [`docs/RESEARCH.md`](docs/RESEARCH.md). В частности, Cassandra не выполняет автоматический перенос token ranges после обычного падения ноды; каскад создают общая перегрузка, hints/recovery и/или исчерпание диска.

Keyspace создан через `NetworkTopologyStrategy` с `dc1: 3`, то есть replication factor явно равен 3. Нагрузка использует CL=ONE, чтобы после потери одной ноды запросы могли продолжаться и перегружать оставшиеся реплики.

## Требования

- Docker Desktop с Linux containers и Compose v2.
- Обычный режим с мониторингом: примерно 6 GiB RAM и 3 CPU для Docker.
- Disk-full режим: желательно 7 GiB RAM. Данные находятся в ограниченных tmpfs и исчезают вместе с контейнерами.
- Первый запуск требует доступа к GitHub/Maven Central/Docker Hub для образов и `windows_exporter`.

Скопируйте `.env.example` в `.env`, только если хотите менять значения по умолчанию.

## Управляемая демонстрация (рекомендуется)

```powershell
.\scripts\start.ps1
.\scripts\load.ps1 -Duration 30m -Threads 48
```

`start.ps1` также запускает VictoriaMetrics, Grafana, cAdvisor и portable `windows_exporter`. Windows exporter скачивается из официального GitHub release в `.tools` и запускается без установки службы. Если Windows Firewall спросит про доступ, разрешите private networks: контейнер VictoriaMetrics обращается к нему через `host.docker.internal:9182`.

В отдельном терминале:

```powershell
.\scripts\watch.ps1
```

Готовый dashboard: <http://localhost:33000/d/cassandra-compaction-debt>. VictoriaMetrics VMUI: <http://localhost:8428/vmui/>. cAdvisor UI: <http://localhost:38080/>. В Grafana включён anonymous Viewer; для редактирования войдите как `admin` / `admin`. Внешние порты Grafana и cAdvisor можно изменить параметрами `GRAFANA_PORT` и `CADVISOR_PORT` в `.env`.

Ожидаемый признак: `pending tasks` и `SSTable count` растут, а ETA compaction не успевает уменьшаться. Если backlog не растёт, перезапустите loader с 96 потоками; если Cassandra начинает падать слишком рано, используйте 24 потока.

Когда backlog отчётливо растёт, имитируйте потерю первой ноды:

```powershell
.\scripts\fail-node.ps1 -Node cassandra1 -Mode Kill
```

`Pause` полезен для демонстрации процесса, который жив, но не отвечает:

```powershell
.\scripts\fail-node.ps1 -Node cassandra1 -Mode Pause
```

После потери ноды следите за Cassandra status, client latency/error rate, hints и backlog оставшихся нод. RF=3 и CL=ONE позволяют нагрузке продолжаться после потери одной ноды.

Остановить только входящий поток и наблюдать восстановление:

```powershell
.\scripts\stop-load.ps1
```

## Естественный каскад через нехватку места

Этот режим заменяет volumes на tmpfs: 550, 800 и 1050 MiB. Первая нода также получает меньше CPU. По мере записи сначала должен исчерпаться filesystem cassandra1, затем cassandra2 и cassandra3. Стандартный `disk_failure_policy: stop` прекращает transport/gossip после фатальной ошибки записи.

```powershell
.\scripts\start.ps1 -DiskFull
.\scripts\load.ps1 -DiskFull -Duration 30m -Threads 48
.\scripts\watch.ps1 -DiskFull
```

Этот вариант более эффектный, но менее детерминированный: Docker Desktop учитывает tmpfs в памяти контейнера, поэтому при слишком маленьком общем memory limit возможен exit 137 раньше ожидаемого disk-full.

## Пять нод

```powershell
.\scripts\start.ps1 -Nodes 5
.\scripts\load.ps1
.\scripts\watch.ps1 -Nodes 5
```

RF остаётся равным 3. Disk-full override рассчитан только на три ноды.

## Мониторинг

- На каждой Cassandra-ноде работает Prometheus JMX Java agent на порту 7070. Для отладки порты опубликованы как 7071..7075.
- VictoriaMetrics хранит метрики за одни сутки и сама выполняет Prometheus scrape каждые 5 секунд.
- `windows_exporter` на Windows-хосте отдаёт CPU, память, физические и логические диски на порту 9182.
- cAdvisor показывает CPU, working set memory и network I/O каждого Docker-контейнера. Docker Desktop не относит usage подключённых volumes/tmpfs к writable layer контейнера, поэтому filesystem Cassandra измеряется лёгким exporter внутри каждой ноды.
- Панели `Cassandra data filesystem used/available/used bytes` показывают точное состояние `/var/lib/cassandra`, включая bounded tmpfs сценария `-DiskFull`.
- Grafana автоматически получает datasource и dashboard для compaction debt, SSTable, клиентских операций и Windows host metrics.
- `node-exporter` сохранён как опциональный профиль. В Docker Desktop он показывает Linux VM Docker, а не сам Windows-хост.

В `/api/v1/targets` VictoriaMetrics неактивные опциональные цели (`cassandra4`, `cassandra5`, `node-exporter`) отображаются как `DOWN`, пока соответствующий profile не запущен. Это ожидаемо и позволяет включать профили без перезапуска VictoriaMetrics.

Запустить также Linux node-exporter:

```powershell
.\scripts\start.ps1 -NodeExporter
```

Запустить Cassandra без Grafana/VictoriaMetrics/windows_exporter:

```powershell
.\scripts\start.ps1 -NoMonitoring
```

Остановить только запущенный этим стендом portable Windows exporter:

```powershell
.\scripts\stop-windows-exporter.ps1
```

## Полезные команды

```powershell
docker compose logs -f loader
docker compose logs -f cassandra1 cassandra2 cassandra3
docker compose exec cassandra2 nodetool status
docker compose exec cassandra2 nodetool tpstats
docker compose exec cassandra2 nodetool netstats
```

Каждая Cassandra-нода пишет в лог блок `LAB_METRIC` с `compactionstats` и заполнением data filesystem. Интервал регулируется `LAB_REPORT_INTERVAL`.

## Сброс

Команда удаляет только контейнеры, сеть и volumes проекта `cassandra-compaction-lab`:

```powershell
.\scripts\reset.ps1 -Force
```

Для tmpfs-сценария передайте `-DiskFull`; после `down` его данные в любом случае невосстановимы.

## Что считать успешным воспроизведением

1. До нагрузки `pending tasks` близко к нулю.
2. Под нагрузкой `pending tasks` и SSTable count имеют устойчивый восходящий тренд.
3. Растут client latency и время ожидания compaction.
4. После потери cassandra1 оставшиеся реплики продолжают принимать запросы, но быстрее накапливают operational debt/hints либо начинают отдавать timeout.
5. После остановки loader восстановление занимает заметно больше времени, чем краткий пик нагрузки.

Результат зависит от диска и CPU Docker Desktop. Важна не конкретная цифра pending tasks, а смена устойчивого состояния: после снятия нагрузки backlog не исчезает сразу.
