"""Append the verified historical Grafana RPS replay to the CUSUM report."""
import html
import json
from pathlib import Path
import shutil


def append_grafana_replay(output):
    replay = Path(__file__).resolve().parents[2] / 'infra/influxdb/cusum-rps'
    manifest = replay / 'metadata.json'
    if not manifest.exists():
        return None
    metadata = json.loads(manifest.read_text(encoding='utf-8'))
    screenshot = replay / metadata['screenshot']
    if not screenshot.exists():
        return None
    verification = json.loads((replay / 'verification.json').read_text(encoding='utf-8'))
    assert verification['influxdb_exact_match_points'] == metadata['points']
    assert verification['grafana_exact_match_points'] == metadata['points']
    assert verification['sample_step_seconds'] == metadata['sample_step_seconds'] == 15
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    assets = {
        screenshot.name: screenshot,
        'real-system-rps-15s.csv': replay / 'rps.csv',
        'real-system-rps-15s-metadata.json': manifest,
        'real-system-rps-15s-verification.json': replay / 'verification.json',
        'real-system-rps-dashboard.json': replay / 'cusum-rps-20260922.json',
    }
    for name, source in assets.items():
        shutil.copyfile(source, output / name)
    row = {**metadata, 'verification': verification}
    article = (
        '<article id="real-system-grafana"><h2>Реальная система: RPS в InfluxDB и Grafana, шаг 15 секунд</h2>'
        '<p>Период: <strong>22.09.2026 13:00:00 — 23.09.2026 10:32:30 МСК</strong> '
        f'(Europe/Moscow). В InfluxDB записана <strong>{metadata["points"]} точка</strong> '
        'с шагом 15 секунд. Метрика <code>rps</code>, поле <code>value</code>, '
        'bucket <code>metrics-replay</code>, тег <code>replay=cusum-real-system-image</code>.</p>'
        '<p>Ниже — снимок настоящей панели Grafana, читающей этот ряд из InfluxDB без усреднения. '
        'Все 5171 значение и временные метки сверены с CSV напрямую в InfluxDB и через источник данных Grafana.</p>'
        f'<p><a href="{html.escape(metadata["dashboard_url"], quote=True)}" target="_blank" rel="noopener">'
        'Открыть интерактивную панель Grafana</a></p>'
        f'<img src="{html.escape(screenshot.name, quote=True)}" '
        'alt="Снимок Grafana: восстановленный RPS за 22–23 сентября 2026 года, шаг 15 секунд" loading="lazy">'
        '<p>Зелёная линия восстановлена по пикселям исходного изображения. Основной уровень принят равным '
        '<strong>1000 RPS</strong>; вертикальный масштаб условный: <strong>1 RPS на пиксель</strong>, '
        'как в предыдущем разделе. Для сетки 15 секунд использована линейная интерполяция пиксельного ряда, '
        'а не минутного CSV. Это приближённая реконструкция: исходные значения и скрытая шкала Y неизвестны; '
        'увеличение плотности точек не восстанавливает отсутствующую телеметрию. '
        'Жёлтая линия и красная рамка не включены в метрику.</p>'
        '<p>Предыдущий прогон детектора относится к шагу 1 минута. Эта панель показывает только RPS; '
        'настройки и результаты детектора не изменялись.</p>'
        '<p><a href="real-system-rps-15s.csv">RPS CSV, шаг 15 секунд</a> · '
        '<a href="real-system-rps-15s-metadata.json">Параметры восстановления</a> · '
        '<a href="real-system-rps-15s-verification.json">Проверка InfluxDB / Grafana</a> · '
        '<a href="real-system-rps-dashboard.json">JSON панели Grafana</a></p></article>'
    )
    return row, article
