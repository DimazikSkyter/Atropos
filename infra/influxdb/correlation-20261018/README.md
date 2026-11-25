# Примеры корреляционного детектора: 18.10.2026

Приближённая оцифровка двух изображений пользователя. Оба графика начинаются
18.10.2026 по Москве; read operations продолжается после полуночи 19 октября.
Восстановлены 3 + 4 отдельно различимые линии, всего 4 284 точки.
Оцифровка сохраняет оси и видимые изменения; скрытые линии не выдумываются.
Метаданные содержат калибровку, хеши изображений и размер интерполированных пропусков.

* InfluxDB: `atropos / metrics-replay / correlation_screenshot_replay`.
* Field: `value`; tags: `scenario` (`latency`, `read_operations`), `trace` (цвет).
* Шаг: 15 с у latency (ms), 60 с у read_operations (ops/s).
* [Grafana](http://localhost:3001/d/correlation-change-20261018): у каждой панели
  есть ссылка «Полный масштаб этого графика» с её исходным диапазоном времени.

Из этой папки, используя Python с NumPy, Pillow и Matplotlib:

```powershell
python digitize_replays.py
python publish_replays.py --write
```

Первая команда читает сохранённые `source/*.png`. Вторая отказывается
перезаписывать конфликтующие существующие значения, добавляет недостающие
точки, сверяет все значения через Flux и datasource Grafana. Обычный запуск
без `--write` выполняет проверку. `metrics.lp` также автоматически используется
существующим seed-загрузчиком Atropos.

Тесты примеров и генератор отчёта читают данные непосредственно из БД:
`detectors/tests/test_correlation_change_influx.py` и
`detectors/tests/visualize_correlation_change.py`. CSV в этой папке нужен для
импорта и аудита оцифровки, а не как подмена входов интеграционных тестов.
Описание алгоритма и параметры прогонов: `detectors/CORRELATION_CHANGE.md`.
