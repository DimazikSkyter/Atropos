"""Collect individual replay dashboards into one overview (Python stdlib only)."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent
DASHBOARDS = ROOT / "dashboards"
UID = "influxdb-replay"

def epoch_ms(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)

def build():
    sources = []
    for path in sorted(DASHBOARDS.glob("*.json")):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        if dashboard.get("uid") != UID:
            sources.append(dashboard)
    sources.sort(key=lambda source: (source["time"]["from"], source["uid"]))
    if not sources:
        raise ValueError("No replay dashboards found")
    panels, links, variables = [], [], {}
    top, next_id = 0, 1
    for source in sources:
        start = datetime.fromisoformat(source["time"]["from"].replace("Z", "+00:00"))
        prefix = start.strftime("%d.%m.%Y")
        details = f"/d/{source['uid']}?from={epoch_ms(source['time']['from'])}&to={epoch_ms(source['time']['to'])}&timezone=Europe%2FMoscow"
        links.append({"type": "link", "title": f"{prefix} · {source['title']}", "url": details, "targetBlank": False, "icon": "external link"})
        panels.append({"id": next_id, "type": "row", "title": f"{prefix} · {source['title']}",
                       "collapsed": False, "panels": [], "gridPos": {"h": 1, "w": 24, "x": 0, "y": top}})
        next_id += 1
        top += 1
        for original in source["panels"]:
            if original["type"] == "row":
                continue
            panel = deepcopy(original)
            panel["id"] = next_id
            panel["gridPos"] = {"h": original["gridPos"]["h"], "w": 24, "x": 0, "y": top}
            panel["links"] = [{"title": "Детальный график: исходный диапазон времени", "url": details, "targetBlank": False}]
            panels.append(panel)
            top += panel["gridPos"]["h"]
            next_id += 1
        for variable in source.get("templating", {}).get("list", []):
            if variable["name"] in variables and variables[variable["name"]] != variable:
                raise ValueError(f"Conflicting variable: {variable['name']}")
            variables[variable["name"]] = deepcopy(variable)
    overview = {
        "id": None, "uid": UID, "title": "InfluxDB Replay", "schemaVersion": 41, "version": 1,
        "editable": True, "timezone": "Europe/Moscow", "refresh": "", "graphTooltip": 1,
        "fiscalYearStartMonth": 0, "annotations": {"list": []}, "tags": ["atropos", "influxdb", "replay", "overview"],
        "description": "Обзор всех восстановленных графиков InfluxDB. Для детализации выберите ссылку на отдельный график с его исходным диапазоном времени. Новые секции собираются build_overview.py из файлов dashboards/*.json.",
        "time": {"from": min(source["time"]["from"] for source in sources), "to": max(source["time"]["to"] for source in sources)},
        "timepicker": {"hidden": False}, "templating": {"list": list(variables.values())},
        "links": links, "panels": panels,
    }
    (DASHBOARDS / "influxdb-replay.json").write_text(json.dumps(overview, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(f"InfluxDB Replay: {len(sources)} dashboards, {len(panels)} panels/rows")

if __name__ == "__main__":
    build()
