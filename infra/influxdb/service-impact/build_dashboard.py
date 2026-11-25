"""Add service response time and error panels to the existing Cassandra/TPS dashboard."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATH = ROOT.parent / "dashboards/cassandra-tps-correlation.json"
DS = {"type": "influxdb", "uid": "influxdb-replay"}
COLORS = ["#73BF69", "#E0B400", "#B6B9BD", "#F2495C", "#B3A84B"]


def panel(pid, title, field, unit, x):
    query = ('import "date"\nfrom(bucket: v.defaultBucket)\n'
             '  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))\n'
             f'  |> filter(fn: (r) => r._measurement == "cassandra_service_impact" and r._field == "{field}")\n'
             '  |> keep(columns: ["_time", "_value", "service"])\n'
             '  |> group(columns: ["service"])\n  |> sort(columns: ["_time"])')
    return {
        "id": pid, "type": "timeseries", "title": title, "datasource": DS,
        "description": "Синтетический сценарий service1–service5, согласованный с историей Cassandra/TPS. Нормальный ВО ≈300 мс. Ошибки = входящий TPS − успешный TPS; до 08:41:15 ошибок нет. Точки каждые 15 секунд без усреднения.",
        "gridPos": {"x": x, "y": 12, "w": 12, "h": 12}, "interval": "15s", "maxDataPoints": 10000,
        "targets": [{"refId": "A", "datasource": DS, "query": query}],
        "fieldConfig": {
            "defaults": {"unit": unit, "min": 0, **({"max": 800} if unit == "ops" else {}), "decimals": 0 if unit == "ms" else 1,
                         "displayName": "${__field.labels.service}",
                         "custom": {"drawStyle": "line", "lineInterpolation": "linear" if unit == "ms" else "stepAfter",
                                    "lineWidth": 1, "fillOpacity": 5, "showPoints": "never", "spanNulls": False,
                                    "scaleDistribution": {"type": "linear"}, "stacking": {"mode": "none", "group": "A"},
                                    "hideFrom": {"tooltip": False, "viz": False, "legend": False}}},
            "overrides": [{"matcher": {"id": "byName", "options": f"service{i}"}, "properties": [
                {"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]} for i, color in enumerate(COLORS, 1)]},
        "options": {"legend": {"showLegend": True, "displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi", "sort": "none", "hideZeros": False}}}


def build():
    dashboard = json.loads(PATH.read_text(encoding="utf-8"))
    original = {p["id"]: p for p in dashboard["panels"]}
    original[1]["gridPos"].update(x=0, y=0, w=12, h=12)
    original[2]["gridPos"].update(x=12, y=0, w=12, h=12)
    note = original[3]
    note["gridPos"].update(y=24, h=12)
    marker = "\n\n### Сервисы service1–service5"
    note["options"]["content"] = note["options"]["content"].split(marker)[0] + marker + (
        "\n\n**ВО и ошибки — синтетический иллюстративный сценарий, а не измерения сервисов.** "
        "Пять TPS-серий сопоставлены service1–service5 по порядку: green, yellow, gray, red, olive.\n\n"
        "До 07:00 ВО около 300 мс (средние сервисов 250–350 мс), без скачка в 22:13–22:14. "
        "Отказ cassandra-1 в 04:53:30 не меняет ВО и не вызывает ошибок. "
        "В 07:00 ВО скачком растёт и затем колеблется: это заданный маркер деградации второй ноды. "
        "С 08:41:15, одновременно с падением успешного TPS, появляются ошибки.\n\n"
        "**Ошибки/с + успешные запросы/с = входящие запросы/с.** "
        "Входящий TPS после падения удерживается около исходного уровня. Поэтому ошибки восполняют потерянный успешный TPS, "
        "а после 12:30 достигают полного уровня входящей нагрузки; ВО отражает тайм-ауты около 5 секунд. "
        "У service4 успешный TPS почти сохраняется, поэтому до окончательного отказа ошибок мало. "
        "Время — Москва, 01–02.10.2026.")
    dashboard["panels"] = [original[1], original[2],
                           panel(4, "ВО · service1–service5", "response_time_ms", "ms", 0),
                           panel(5, "Ошибки · service1–service5", "errors_tps", "ops", 12), note]
    dashboard["description"] = "Cassandra, успешный TPS, синтетические ВО и ошибки service1–service5 в общем диапазоне времени."
    dashboard["graphTooltip"] = 1
    dashboard["tags"] = sorted(set(dashboard.get("tags", [])) | {"services", "synthetic"})
    PATH.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Added service response time and error panels to cassandra-tps-correlation")


if __name__ == "__main__":
    build()
