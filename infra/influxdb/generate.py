"""Resample the digitized reference at 15 s; uses only the Python standard library."""
from __future__ import annotations

import argparse
import bisect
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOSTS = [f"cassandra-{i}" for i in range(1, 5)]
STEP_SECONDS = 15
DURATION_SECONDS = 86400
MSK = timezone(timedelta(hours=3))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="2026-10-01", help="First day in Moscow time (YYYY-MM-DD)")
    args = parser.parse_args()
    start = datetime.fromisoformat(args.date).replace(hour=12, minute=30, tzinfo=MSK)
    end = start + timedelta(seconds=DURATION_SECONDS)
    with (ROOT / "reference-traces.csv").open(encoding="utf-8", newline="") as source:
        reference = list(csv.DictReader(source))
    positions = [float(row["elapsed_seconds"]) for row in reference]
    counts = {host: 0 for host in HOSTS}
    with (ROOT / "cassandra-response-time.lp").open("w", encoding="utf-8", newline="\n") as lp, \
         (ROOT / "cassandra-response-time.csv").open("w", encoding="utf-8", newline="") as output:
        csv_writer = csv.writer(output)
        csv_writer.writerow(["timestamp", "host", "cassandra_response_time_ms"])
        for offset in range(0, DURATION_SECONDS + 1, STEP_SECONDS):
            index = max(0, bisect.bisect_right(positions, offset) - 1)
            timestamp = start + timedelta(seconds=offset)
            for host in HOSTS:
                value = float(reference[index][host])
                if not 0 <= value <= 40:
                    raise ValueError(f"Unexpected value for {host}: {value}")
                lp.write(f"cassandra_response_time,host={host} value={value:.3f} {int(timestamp.timestamp())}\n")
                csv_writer.writerow([timestamp.isoformat(), host, f"{value:.3f}"])
                counts[host] += 1
    dashboard_path = ROOT / "dashboards" / "cassandra-response-time.json"
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    dashboard["time"] = {
        "from": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "to": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    dashboard_path.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    metadata = {
        "measurement": "cassandra_response_time", "field": "value", "unit": "ms",
        "tag": "host", "hosts": HOSTS, "start": start.isoformat(), "end": end.isoformat(),
        "timezone": "Europe/Moscow", "interval_seconds": STEP_SECONDS,
        "points_per_host": counts, "total_points": sum(counts.values()),
        "source": "reference.png", "source_columns": 1603, "source_trace_points": len(reference),
        "reconstruction": "Approximate digitization of four colored traces; occlusions and 15-second intervals filled with preceding values. High plateaus calibrated from the visible grid. Endpoints of dense vertical strokes placed in adjacent 15-second slots; subpixel timing is estimated."
    }
    (ROOT / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
