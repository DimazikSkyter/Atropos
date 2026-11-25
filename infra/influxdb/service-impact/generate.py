"""Generate a reproducible synthetic service scenario aligned to the Cassandra/TPS replay."""
import csv
import json
import math
import statistics
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEASUREMENT = "cassandra_service_impact"
FIELDS = ("response_time_ms", "errors_tps", "successful_tps", "offered_tps")
MAPPING = dict(zip((f"service{i}" for i in range(1, 6)), ("green", "yellow", "gray", "red", "olive")))


def generate():
    source_metadata = json.loads((ROOT.parent / "cassandra-tps/metadata.json").read_text(encoding="utf-8"))
    start = datetime.fromisoformat(source_metadata["start"])
    end = datetime.fromisoformat(source_metadata["end"])
    partial = datetime.fromisoformat(source_metadata["partial_drop_time"])
    terminal = datetime.fromisoformat(source_metadata["zero_time"])
    onset = partial.replace(hour=7, minute=0, second=0, microsecond=0)
    first = datetime.fromisoformat(next(e["time"] for e in source_metadata["events"] if e["name"] == "first_node_failure"))
    source = {}
    with (ROOT.parent / "cassandra-tps/metrics.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            source.setdefault(row["trace"], {})[datetime.fromisoformat(row["timestamp"])] = float(row["cassandra_tps"])
    rates = {service: statistics.fmean(v for t, v in source[trace].items() if start <= t < start + timedelta(hours=2))
             for service, trace in MAPPING.items()}
    count = 0
    with (ROOT / "metrics.csv").open("w", encoding="utf-8", newline="") as table, (ROOT / "metrics.lp").open("w", encoding="utf-8", newline="\n") as lp:
        writer = csv.writer(table)
        writer.writerow(["timestamp", "service", "source_trace", *FIELDS])
        for t in sorted(source["green"]):
            elapsed = (t - start).total_seconds()
            common = 9 * math.sin(elapsed / 640) + 7 * math.sin(elapsed / 153) + 3 * math.sin(elapsed / 49)
            for i, (service, trace) in enumerate(MAPPING.items()):
                baseline = 250 + 25 * i + common + 5 * math.sin(elapsed / 217 + i * .85)
                # There is deliberately no event at 22:13 or at the first node failure.
                if t < onset:
                    latency = baseline
                else:
                    # A clear jump at 07:00 and growing, correlated oscillations as the second node hangs.
                    seconds = (t - onset).total_seconds()
                    growth = min(seconds / (partial - onset).total_seconds(), 1.0)
                    wave = .88 + .10 * math.sin(seconds / 340) + .05 * math.sin(seconds / 91 + i * .2)
                    latency = baseline + (600 + 210 * i + (800 + 130 * i) * growth) * wave
                    if t >= partial:
                        latency += 400 + 80 * i + 180 * math.sin(seconds / 480 + i * .25)
                    if t >= terminal:
                        # Requests continue and return timeouts even when successful TPS is zero.
                        latency = 4800 + 100 * i + 50 * math.sin(seconds / 137 + i * .3)
                successful = source[trace][t]
                if t < partial:
                    offered, errors = successful, 0.0
                else:
                    offered = max(successful, round(rates[service] * (1 + .006 * math.sin(elapsed / 520 + i * .4)), 3))
                    errors = round(offered - successful, 3)
                values = [round(latency, 3), errors, successful, offered]
                writer.writerow([t.isoformat(), service, trace, *(f"{v:.3f}" for v in values)])
                fields = ",".join(f"{name}={v:.3f}" for name, v in zip(FIELDS, values))
                lp.write(f"{MEASUREMENT},service={service} {fields} {int(t.timestamp())}\n")
                count += 1
    metadata = {
        "measurement": MEASUREMENT, "fields": list(FIELDS), "service_to_tps_trace": MAPPING,
        "provenance": "Synthetic illustrative service metrics; existing Cassandra and TPS histories are unchanged.",
        "start": start.isoformat(), "end": end.isoformat(), "timezone": "Europe/Moscow", "interval_seconds": 15,
        "series_points": count, "field_values": count * len(FIELDS), "points_per_service": count // 5,
        "events": {"first_node_failure_no_service_effect": first.isoformat(), "response_jump": onset.isoformat(),
                   "errors_start_and_tps_drop": partial.isoformat(), "successful_tps_zero": terminal.isoformat()},
        "baseline_response_ms": dict(zip(MAPPING, (250, 275, 300, 325, 350))),
        "offered_tps_reference": rates,
        "error_model": "Before the TPS drop: zero errors. Afterwards: errors = offered TPS - successful TPS; offered TPS stays near the initial two-hour level. After 12:30 all offered requests fail.",
        "assumptions": "07:00 is the requested scenario marker near the second node's degradation, not a measured outage timestamp. Service identities and their mapping to the screenshot's colored TPS traces are illustrative. Response times include failed requests."
    }
    (ROOT / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {count} service samples / {count * len(FIELDS)} field values")


if __name__ == "__main__":
    generate()
