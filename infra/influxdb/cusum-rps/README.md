# RPS: реконструкция графика реальной системы

Восстановленная зелёная линия из исходного изображения CUSUM, основной уровень
1000 RPS. Начало — 22.09.2026 13:00:00 МСК, конец — 23.09.2026 10:32:30 МСК.
5171 точка с шагом 15 секунд. Метрика `rps`, поле `value`, тег
`replay=cusum-real-system-image`, organization `atropos`, bucket `metrics-replay`.

Шкала Y на изображении скрыта. Сохранён условный масштаб предыдущего отчёта:
1 RPS на вертикальный пиксель. Используется нижняя граница зелёного следа
(98-й процентиль), с линейной интерполяцией непосредственно пиксельного ряда.
Красная рамка и нижняя жёлтая линия исключены. Данные приближённые; новый шаг
не добавляет информации, отсутствующей на изображении.

`generate.py` читает `detectors/tests/fixtures/real-system-pixels.csv` и калибровку
`real-system-replay.json`. Из корня репозитория:

```powershell
detectors/.venv/Scripts/python.exe infra/influxdb/cusum-rps/generate.py
detectors/.venv/Scripts/python.exe infra/influxdb/cusum-rps/publish_verify.py
```

Второй скрипт записывает ряд, создаёт панель с UID `cusum-rps-20260922`, если её
ещё нет, и сверяет все значения через InfluxDB API и Grafana API. Повторная
запись с теми же метрикой, тегами и временными метками не создаёт дубликаты.
Параметры подключения берутся из переменных `INFLUX_HOST`, `INFLUXDB_ORG`,
`INFLUXDB_BUCKET`, `INFLUXDB_TOKEN`, `GRAFANA_URL`, `GRAFANA_ADMIN_USER`,
`GRAFANA_ADMIN_PASSWORD`; значения по умолчанию совпадают с monitoring compose.
Этот replay настроен на org/bucket из compose по умолчанию.

При запуске compose `influxdb-seed` импортирует вложенный `rps.lp` автоматически.
Копия JSON в `infra/influxdb/dashboards/cusum-rps-20260922.json` обеспечивает
файловое provisioning панели. При изменении генератора нужно обновить эту копию.

`real-system-grafana.jpg` — снимок работающей Grafana, а не нарисованный отдельно
график. `verification.json` подтверждает совпадение всех 5171 значений и шага 15 с.
Сборка отчёта `python -m tests.visualize_cusum` из каталога `detectors` добавляет
снимок последним разделом, если присутствуют эти replay-артефакты. Снимок является
статическим и при изменении данных должен быть снят заново.
