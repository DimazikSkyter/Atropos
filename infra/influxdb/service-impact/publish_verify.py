"""Validate, snapshot, import and check the local Cassandra service replay."""
import argparse
import base64
import csv
import hashlib
import io
import json
import math
import os
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

os.environ.pop("SSLKEYLOGFILE", None)
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
METRIC = "cassandra_service_impact"
INFLUX = os.getenv("INFLUX_URL", "http://127.0.0.1:8086").rstrip("/")
ORG = os.getenv("INFLUXDB_ORG", "atropos")
BUCKET = os.getenv("INFLUXDB_BUCKET", "metrics-replay")
TOKEN = os.getenv("INFLUXDB_TOKEN", "atropos-local-replay-token")
GRAFANA = os.getenv("GRAFANA_URL", "http://127.0.0.1:3001").rstrip("/")
AUTH = {"Authorization": "Basic " + base64.b64encode((os.getenv("GRAFANA_ADMIN_USER", "admin") + ":" + os.getenv("GRAFANA_ADMIN_PASSWORD", "admin")).encode()).decode()}


def request(url, data=None, headers=None):
    with urlopen(Request(url, data=data, headers=headers or {}), timeout=60) as response:
        return response.read().decode()


def read_database():
    flux = f'from(bucket: {json.dumps(BUCKET)}) |> range(start: 0)'
    raw = request(INFLUX + "/api/v2/query?" + urlencode({"org": ORG}), flux.encode(),
                  {"Authorization": "Token " + TOKEN, "Content-Type": "application/vnd.flux", "Accept": "application/csv"})
    points, header = {}, None
    for fields in csv.reader(io.StringIO(raw)):
        if not fields or fields[0].startswith("#"):
            continue
        if "_time" in fields:
            header = fields
            continue
        if header is None:
            raise ValueError("Influx query returned no header")
        row = dict(zip(header, fields))
        if not row.get("_time"):
            continue
        tags = tuple(sorted((k, v) for k, v in row.items() if k not in
                            {"", "result", "table", "_start", "_stop", "_time", "_value", "_field", "_measurement"} and v))
        identity = (row["_measurement"], row["_field"], row["_time"], tags)
        assert identity not in points, "Duplicate returned point"
        points[identity] = row["_value"]
    return points


def summary(points):
    return {metric: {"field_values": len(part), "sha256": hashlib.sha256(json.dumps(sorted(part), separators=(",", ":")).encode()).hexdigest()}
            for metric in sorted({key[0] for key in points})
            for part in [[(key, value) for key, value in points.items() if key[0] == metric]]}


def local_checks():
    meta = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    rows, series, expected = [], {}, {}
    for row in csv.DictReader((ROOT / "metrics.csv").open(encoding="utf-8", newline="")):
        t = datetime.fromisoformat(row["timestamp"])
        values = {name: float(row[name]) for name in meta["fields"]}
        sample = (t, values)
        series.setdefault(row["service"], []).append(sample)
        rows.append(sample)
        for field, value in values.items():
            expected[(field, int(t.timestamp()), row["service"])] = value
    onset = datetime.fromisoformat(meta["events"]["response_jump"])
    partial = datetime.fromisoformat(meta["events"]["errors_start_and_tps_drop"])
    terminal = datetime.fromisoformat(meta["events"]["successful_tps_zero"])
    first = datetime.fromisoformat(meta["events"]["first_node_failure_no_service_effect"])
    def average(points, begin, end):
        return statistics.fmean(v["response_time_ms"] for t, v in points if begin <= t < end)
    normal = [v["response_time_ms"] for t, v in rows if t < onset]
    per_service = {}
    for service, points in series.items():
        indexed = dict(points)
        per_service[service] = {
            "baseline_response_ms": average(points, points[0][0], onset),
            "jump_at_0700_ms": indexed[onset]["response_time_ms"] - indexed[onset - timedelta(seconds=15)]["response_time_ms"],
            "response_ratio_around_first_failure": average(points, first, first + timedelta(minutes=15)) / average(points, first - timedelta(minutes=15), first),
            "errors_at_tps_drop": indexed[partial]["errors_tps"],
            "errors_after_total_failure": indexed[terminal]["errors_tps"]}
    reference = {(int(datetime.fromisoformat(r["timestamp"]).timestamp()), r["trace"]): float(r["cassandra_tps"])
                 for r in csv.DictReader((ROOT.parent / "cassandra-tps/metrics.csv").open(encoding="utf-8"))}
    checks = {
        "five_named_services": set(series) == set(f"service{i}" for i in range(1, 6)),
        "complete_uniform_15s_series": all(len(p) == meta["points_per_service"] and all((b[0]-a[0]).total_seconds() == 15 for a,b in zip(p,p[1:])) for p in series.values()),
        "normal_response_average_approximately_300ms": abs(statistics.fmean(normal) - 300) < 5,
        "first_node_failure_has_no_response_level_step": all(abs(s["response_ratio_around_first_failure"] - 1) < .05 for s in per_service.values()),
        "clear_response_jump_at_0700": all(s["jump_at_0700_ms"] > 500 for s in per_service.values()),
        "no_2213_2214_jump": all(abs(b[1]["response_time_ms"]-a[1]["response_time_ms"]) < 10 for p in series.values() for a,b in zip(p,p[1:]) if a[0].hour == 22 and a[0].minute in (12,13,14)),
        "errors_zero_before_tps_drop": all(v["errors_tps"] == 0 for t,v in rows if t < partial),
        "positive_errors_begin_exactly_at_tps_drop": min(t for t,v in rows if v["errors_tps"] > 0) == partial,
        "request_rates_conserved_at_every_sample": all(abs(v["successful_tps"]+v["errors_tps"]-v["offered_tps"]) < .00001 for _,v in rows),
        "terminal_errors_cover_full_offered_tps": all(v["successful_tps"] == 0 and v["errors_tps"] == v["offered_tps"] and v["errors_tps"] > 0 for t,v in rows if t >= terminal),
        "success_tps_exactly_matches_existing_traces": all(v["successful_tps"] == reference[(int(t.timestamp()),meta["service_to_tps_trace"][service])] for service,p in series.items() for t,v in p),
        "all_values_nonnegative_finite": all(math.isfinite(v) and v >= 0 for _,values in rows for v in values.values()),
        "expected_point_count": len(expected) == meta["field_values"]}
    return expected, meta, checks, {"normal_response_mean_ms": statistics.fmean(normal), "services": per_service}


def normalize_new(points):
    return {(key[1], int(datetime.fromisoformat(key[2].replace("Z", "+00:00")).timestamp()), dict(key[3])["service"]): float(value)
            for key,value in points.items() if key[0] == METRIC}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["draft", "before", "write", "after"])
    phase = parser.parse_args().phase
    expected, meta, checks, stats = local_checks()
    assert all(checks.values()), {k:v for k,v in checks.items() if not v}
    if phase == "draft":
        result = {"passed": True, "checks": checks, "statistics": stats}
        (ROOT / "draft-verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
        return
    if phase == "before":
        current = read_database()
        assert METRIC not in summary(current), "Scenario already exists; inspect saved verification before changing it"
        # The source used for the scenario must match the running database.
        reference = {(int(datetime.fromisoformat(r["timestamp"]).timestamp()),r["trace"]): float(r["cassandra_tps"])
                     for r in csv.DictReader((ROOT.parent / "cassandra-tps/metrics.csv").open(encoding="utf-8"))}
        live = {(int(datetime.fromisoformat(k[2].replace("Z", "+00:00")).timestamp()),dict(k[3])["trace"]):float(v)
                for k,v in current.items() if k[0] == "cassandra_tps" and k[1] == "value"}
        assert reference == live, "Live TPS differs from the scenario source"
        cassandra = {(int(datetime.fromisoformat(r["timestamp"]).timestamp()),r["host"]):float(r["cassandra_response_time_ms"])
                     for r in csv.DictReader((ROOT.parent / "cassandra-response-time.csv").open(encoding="utf-8"))}
        live_cassandra = {(int(datetime.fromisoformat(k[2].replace("Z", "+00:00")).timestamp()),dict(k[3])["host"]):float(v)
                          for k,v in current.items() if k[0] == "cassandra_response_time" and k[1] == "value"}
        assert cassandra == live_cassandra, "Live Cassandra history differs from the source"
        snapshot = summary(current)
        (ROOT / "before.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"source_histories_match": True, "measurements": len(snapshot), "field_values": sum(v["field_values"] for v in snapshot.values())}))
        return
    assert (ROOT / "before.json").exists(), "Capture database before importing"
    if phase == "write":
        request(INFLUX + "/api/v2/write?" + urlencode({"org":ORG,"bucket":BUCKET,"precision":"s"}),
                (ROOT / "metrics.lp").read_bytes(), {"Authorization":"Token " + TOKEN,"Content-Type":"text/plain; charset=utf-8"})
        print(f"Imported {meta['series_points']} samples / {len(expected)} field values")
        return
    current = read_database()
    before = json.loads((ROOT / "before.json").read_text(encoding="utf-8"))
    after = summary(current)
    checks["existing_measurements_unchanged"] = all(after.get(name) == info for name,info in before.items())
    checks["database_matches_every_generated_field_value"] = normalize_new(current) == expected
    health = json.loads(request(GRAFANA + "/api/datasources/uid/influxdb-replay/health", headers=AUTH))
    checks["grafana_datasource_healthy"] = health.get("status") == "OK"
    dashboard = json.loads(request(GRAFANA + "/api/dashboards/uid/cassandra-tps-correlation", headers=AUTH))["dashboard"]
    checks["same_dashboard_contains_four_charts"] = [p["id"] for p in dashboard["panels"] if p["type"] == "timeseries"] == [1,2,4,5]
    frames_per_panel = {}
    for pid, field in [(4,"response_time_ms"),(5,"errors_tps")]:
        panel = next(p for p in dashboard["panels"] if p["id"] == pid)
        target = panel["targets"][0]
        payload = {"from":str(int(datetime.fromisoformat(dashboard["time"]["from"].replace("Z","+00:00")).timestamp()*1000)),
                   "to":str(int(datetime.fromisoformat(dashboard["time"]["to"].replace("Z","+00:00")).timestamp()*1000)),
                   "queries":[{**target,"intervalMs":15000,"maxDataPoints":10000}]}
        response = json.loads(request(GRAFANA + "/api/ds/query",json.dumps(payload).encode(),{**AUTH,"Content-Type":"application/json"}))["results"]["A"]
        assert not response.get("error"), response.get("error")
        actual = {}
        for frame in response["frames"]:
            fields = frame["schema"]["fields"]
            ti = next(i for i,f in enumerate(fields) if f["type"] == "time")
            vi = next(i for i,f in enumerate(fields) if f["type"] == "number")
            service = fields[vi]["labels"]["service"]
            times,values = frame["data"]["values"][ti],frame["data"]["values"][vi]
            for t,v in zip(times,values): actual[(field,int(t/1000),service)] = v
        frames_per_panel[pid] = len(response["frames"])
        checks[f"grafana_panel_{pid}_exact_raw_data"] = actual == {k:v for k,v in expected.items() if k[0] == field}
    overview = json.loads(request(GRAFANA + "/api/dashboards/uid/influxdb-replay", headers=AUTH))["dashboard"]
    checks["overview_includes_new_panels"] = all(any(p.get("title") == title for p in overview["panels"]) for title in ["ВО · service1–service5","Ошибки · service1–service5"])
    result = {"passed":all(checks.values()),"checks":checks,"statistics":stats,"field_values_added":len(expected),
              "events":meta["events"],"before":before,"after":after,"grafana_frames":frames_per_panel,
              "grafana_url":GRAFANA + "/d/cassandra-tps-correlation"}
    (ROOT / "verification.json").write_text(json.dumps(result,indent=2) + "\n",encoding="utf-8")
    print(json.dumps({"passed":result["passed"],"checks":checks,"field_values_added":len(expected),"grafana_frames":frames_per_panel},indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
