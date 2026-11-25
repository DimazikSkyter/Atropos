from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent

def query(measurement, status=False):
    result = '''import "date"

from(bucket: v.defaultBucket)
  |> range(start: v.timeRangeStart, stop: date.add(d: 1ns, to: v.timeRangeStop))
  |> filter(fn: (r) => r._measurement == "MEASUREMENT" and r._field == "value")'''.replace("MEASUREMENT", measurement)
    if status:
        result += '\n  |> filter(fn: (r) => r.status == "200")\n  |> keep(columns: ["_time", "_value", "status"])\n  |> group(columns: ["status"])'
    else:
        result += '\n  |> keep(columns: ["_time", "_value"])\n  |> group(columns: [])'
    return result + '\n  |> sort(columns: ["_time"])'

dashboard = {
    "id": None, "uid": "request-handle-time-replay", "version": 1, "schemaVersion": 41,
    "title": "Request handle time and total RPS — replay", "editable": True,
    "timezone": "Europe/Moscow", "refresh": "", "graphTooltip": 1,
    "fiscalYearStartMonth": 0, "annotations": {"list": []}, "links": [],
    "tags": ["atropos", "replay", "influxdb", "rps", "latency"],
    "description": "Approximate digitization of the supplied screenshot. Yellow: request_handle_time, status=200, ms; green: total_rps (~10000), requests/s. Date replaced with 03 Oct 2026. Raw 15-second samples.",
    "time": {"from": "2026-10-03T16:12:00Z", "to": "2026-10-03T20:06:00Z"},
    "templating": {"list": []},
    "panels": [{
        "id": 1, "type": "timeseries", "title": "request_handle_time (200) / total_rps",
        "description": "15-second raw samples; no aggregation. Yellow scale anchored at 13.5 ms from the tooltip; green mean ~10000 RPS. The reference axes were cropped, so values are approximate.",
        "datasource": {"type": "influxdb", "uid": "influxdb-replay"},
        "gridPos": {"h": 17, "w": 24, "x": 0, "y": 0},
        "maxDataPoints": 10000, "interval": "15s",
        "targets": [{"refId": ref, "datasource": {"type": "influxdb", "uid": "influxdb-replay"}, "query": query(metric, ref == "A")}
                    for ref, metric in [("A", "request_handle_time"), ("B", "total_rps")]],
        "fieldConfig": {
            "defaults": {
                "unit": "ms", "min": 0, "max": 54, "decimals": 2,
                "custom": {"drawStyle": "line", "lineInterpolation": "linear", "lineWidth": 1,
                           "fillOpacity": 0, "showPoints": "never", "spanNulls": False,
                           "axisPlacement": "left", "axisLabel": "ms", "axisColorMode": "text",
                           "axisBorderShow": False, "axisCenteredZero": False,
                           "scaleDistribution": {"type": "linear"}, "stacking": {"mode": "none", "group": "A"},
                           "hideFrom": {"tooltip": False, "viz": False, "legend": False}},
            },
            "overrides": [
                {"matcher": {"id": "byFrameRefID", "options": "A"}, "properties": [
                    {"id": "displayName", "value": "request_handle_time (200)"},
                    {"id": "color", "value": {"mode": "fixed", "fixedColor": "#E0B400"}},
                ]},
                {"matcher": {"id": "byFrameRefID", "options": "B"}, "properties": [
                    {"id": "displayName", "value": "total_rps"},
                    {"id": "color", "value": {"mode": "fixed", "fixedColor": "#73BF69"}},
                    {"id": "unit", "value": "reqps"}, {"id": "decimals", "value": 0},
                    {"id": "max", "value": 11600}, {"id": "custom.axisPlacement", "value": "right"},
                    {"id": "custom.axisLabel", "value": "RPS"},
                ]},
            ],
        },
        "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []},
                    "tooltip": {"mode": "multi", "sort": "none", "hideZeros": False}},
    }],
}
(ROOT.parent / "dashboards" / "request-handle-time.json").write_text(json.dumps(dashboard, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
