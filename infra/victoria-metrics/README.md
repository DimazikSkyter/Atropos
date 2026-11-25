# Постоянное хранилище VictoriaMetrics

Оба Compose-файла хранят метрики в папке хоста `F:\vm_data`, подключённой к
`/victoria-metrics-data` контейнера. Папка создаётся автоматически при запуске.
Данные сохраняются при штатной остановке и перезапуске контейнера, Docker и
компьютера, пересоздании контейнера, `docker compose down` и `down --volumes`.
После принудительного завершения или отключения питания последние ещё не
записанные на диск данные могут потеряться. Это хранилище не заменяет резервные копии.

Параметры в `infra/.env` (можно скопировать `infra/.env.example`):

```dotenv
VM_DATA_PATH=F:/vm_data
VM_RETENTION_PERIOD=100y
```

Используйте абсолютный путь; на Windows удобнее прямые слеши, в том числе для
путей с пробелами. Docker Desktop должен иметь доступ к выбранному диску.
`VM_RETENTION_PERIOD` задаёт возраст данных, после которого они удаляются:
100 лет по умолчанию позволяют накапливать историю локальных запусков.
Если в существующем `.env` задано `30d`, обновите его, иначе останется срок 30 дней.
Следите за свободным местом на диске.

Запуск из корня проекта (явный `--env-file` гарантирует выбор нужного файла):

```powershell
docker compose --env-file infra/.env -f infra/docker-compose-monitoring.yml up -d
```

Без `infra/.env` можно запускать `make monitoring_up` с указанными выше значениями
по умолчанию. Для полного стенда используйте `infra/docker-compose.yml` или `make up`.
Не запускайте два экземпляра VictoriaMetrics одновременно с одной папкой данных.
Оба варианта стенда намеренно используют одну историю; при переключении сначала
остановите предыдущий экземпляр. `restart: unless-stopped` восстанавливает работу
после запуска Docker, если контейнер не был остановлен вручную. После ручной
остановки используйте `up -d` или `start`.

## Автоматический перенос при make monitoring_up

`make monitoring_up` перед запуском контейнеров выполняет
`infra/scripts/migrate-victoria-metrics.ps1`. Скрипт получает путь хранения и имя
проекта из итогового `docker compose config`; `infra/.env` автоматически применяется
и при переносе, и при запуске.

- Если папка назначения уже содержит данные, перенос пропускается.
- Если старого volume `<имя-проекта>_victoria_metrics_data` нет или он пуст,
  запуск продолжается без ошибки.
- Перед копированием старый контейнер VictoriaMetrics останавливается. Данные
  копируются только из volume, которым больше не пользуются запущенные контейнеры.
- Исходный volume сохраняется. При ошибке Docker или копирования запуск прерывается,
  чтобы не открыть неполную базу. Маркер `.vm-migration-in-progress` блокирует
  повторный запуск после неудачного копирования. Для повторения сначала сохраните
  частичную папку под другим именем и подготовьте пустую папку назначения.

По умолчанию переносится volume проекта monitoring. Для переноса из другого
проекта явно укажите источник (папка назначения должна быть пустой):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File infra/scripts/migrate-victoria-metrics.ps1 -SourceVolume atropos_victoria_metrics_data
make monitoring_up
```

Для такой операции сначала остановите VictoriaMetrics исходного проекта.
При первом переносе Docker может скачать вспомогательный образ `alpine:3.23`.
Прямой вызов `docker compose up` и `make up` автоматический перенос не выполняет.

## Ручной перенос ранее накопленной истории из Docker volume

Новый путь сам по себе не переносит старые данные. До первого запуска с новой
конфигурацией скопируйте прежний volume в пустую папку назначения. Старые volumes
не удаляйте до проверки результата. Команды ниже для monitoring; для полного
стенда замените Compose-файл и имя volume на фактические.

1. Запустите Docker и найдите старый volume:

   ```powershell
   docker volume ls
   docker volume inspect monitoring_victoria_metrics_data
   ```

   Обычно это `monitoring_victoria_metrics_data` либо
   `atropos_victoria_metrics_data`. При изменённом имени Compose-проекта имя другое.
   Если есть два старых хранилища, не копируйте их друг поверх друга.

2. Остановите VictoriaMetrics перед копированием:

   ```powershell
   docker compose -f infra/docker-compose-monitoring.yml stop victoria-metrics
   ```

3. Создайте пустую папку и скопируйте данные (при другом пути замените оба значения):

   ```powershell
   New-Item -ItemType Directory -Force F:\vm_data
   if (Get-ChildItem -Force F:\vm_data | Select-Object -First 1) { throw 'Папка назначения должна быть пустой' }
   docker run --rm --mount type=volume,source=monitoring_victoria_metrics_data,target=/source,readonly --mount type=bind,source=F:/vm_data,target=/target alpine:3.23 sh -c 'cp -a /source/. /target/'
   if ($LASTEXITCODE -ne 0) { throw 'Ошибка копирования; не запускайте VictoriaMetrics до исправления' }
   ```

4. Запустите Compose с новым путём и проверьте прежний временной диапазон в Grafana
   или VMUI (`http://localhost:8428/vmui`). Затем выполните `restart victoria-metrics`
   и убедитесь, что прежние точки доступны. Для проверки пересоздания используйте
   `up -d --force-recreate victoria-metrics` с теми же Compose-файлом и `.env`.

Срок хранения описан в [документации VictoriaMetrics](https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/#retention).
