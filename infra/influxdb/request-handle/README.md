# Время обработки запросов 200 и суммарный RPS

График входит в общий [InfluxDB Replay](http://localhost:3001/d/influxdb-replay).
[Детальный просмотр](http://localhost:3001/d/request-handle-time-replay) открывает
03.10.2026 **19:12–23:06, Europe/Moscow**, с отсчетами каждые **15 секунд**.

| Линия | Measurement | Tag | Поле / единицы |
| --- | --- | --- | --- |
| Желтая | `request_handle_time` | `status=200` | `value`, ms |
| Зеленая | `total_rps` | нет (суммарная нагрузка) | `value`, requests/s |

По 937 точек на ряд, 1 874 всего. В Grafana отдельные оси: слева ms, справа RPS.
Запросы возвращают сырые точки без усреднения; `maxDataPoints=10000`.

Данные приблизительно восстановлены из `reference.png`. Дата изображения игнорируется.
Оси на скриншоте обрезаны, поэтому масштаб желтой линии откалиброван по tooltip
**13.5 ms**, зеленой — по заданному уровню **около 10 000 RPS**. Короткие положительные
пики времени отклика сохраняются в ближайшем 15-секундном отсчете. Между пикселями
выполнена линейная интерполяция. `calibration.json` описывает калибровку, `metadata.json`
— диапазон, счетчики и характеристики восстановленных рядов.

Перегенерация (стандартная библиотека Python):

```powershell
python infra/influxdb/request-handle/generate.py --date 2026-10-03
python infra/influxdb/build_overview.py
docker compose -f infra/docker-compose-monitoring.yml run --rm influxdb-seed
```

Готовые `metrics.lp` и `metrics.csv` позволяют запускать стенд без Python.
Для повторной оцифровки `digitize_reference.py` нужны `numpy` и `Pillow`,
для статического превью `plot_preview.py` — `matplotlib`.
