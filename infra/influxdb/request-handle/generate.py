"""Create 15-second samples and dashboard from digitized reference (stdlib only)."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import argparse
import csv
import bisect
import json

ROOT = Path(__file__).resolve().parent
MSK = timezone(timedelta(hours=3))
SECONDS, STEP = 14040, 15

def interpolate(rows, positions, offset, field):
    right = bisect.bisect_right(positions, offset)
    if right == 0:
        return float(rows[0][field])
    if right == len(rows):
        return float(rows[-1][field])
    left = right-1
    weight = (offset-positions[left]) / (positions[right]-positions[left])
    return float(rows[left][field])*(1-weight) + float(rows[right][field])*weight

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="2026-10-03")
    args = parser.parse_args()
    start = datetime.combine(date.fromisoformat(args.date), datetime.min.time(), MSK).replace(hour=19, minute=12)
    end = start + timedelta(seconds=SECONDS)
    with (ROOT / "reference-traces.csv").open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    positions = [float(row["elapsed_seconds"]) for row in rows]
    points = []
    with (ROOT / "metrics.lp").open("w", encoding="utf-8", newline="\n") as lp, \
         (ROOT / "metrics.csv").open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["timestamp", "request_handle_time_ms", "status", "total_rps"])
        for offset in range(0, SECONDS+1, STEP):
            # A pixel spans ~8 s. Preserve narrow positive latency peaks inside
            # each 15 s interval instead of averaging them away.
            response = interpolate(rows, positions, offset, "request_handle_time_ms")
            nearby = [float(rows[i]["request_handle_time_ms"]) for i in range(
                bisect.bisect_left(positions, max(0, offset-STEP/2)),
                bisect.bisect_right(positions, min(SECONDS, offset+STEP/2)))]
            if nearby:
                response = max(response, *nearby)
            rps = interpolate(rows, positions, offset, "total_rps")
            response, rps = round(response, 3), round(rps, 3)
            ts = start+timedelta(seconds=offset)
            lp.write(f"request_handle_time,status=200 value={response:.3f} {int(ts.timestamp())}\n")
            lp.write(f"total_rps value={rps:.3f} {int(ts.timestamp())}\n")
            writer.writerow([ts.isoformat(), response, "200", rps])
            points.append((ts.isoformat(), response, rps))
    calibration = json.loads((ROOT / "calibration.json").read_text())
    dashboard = json.loads((ROOT.parent / "dashboards" / "request-handle-time.json").read_text(encoding="utf-8"))
    dashboard["time"] = {"from": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "to": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    dashboard["panels"][0]["fieldConfig"]["defaults"]["max"] = calibration["left_axis_max_ms"]
    for override in dashboard["panels"][0]["fieldConfig"]["overrides"]:
        if override["matcher"]["options"] == "B":
            for prop in override["properties"]:
                if prop["id"] == "max":
                    prop["value"] = calibration["right_axis_max_rps"]
    (ROOT.parent / "dashboards" / "request-handle-time.json").write_text(json.dumps(dashboard, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    metadata = {
        "start": start.isoformat(), "end": end.isoformat(), "timezone": "Europe/Moscow", "interval_seconds": STEP,
        "points_per_metric": len(points), "total_points": len(points)*2,
        "metrics": {"request_handle_time": {"unit": "ms", "tags": {"status": "200"}, "min": min(p[1] for p in points), "max": max(p[1] for p in points)},
                    "total_rps": {"unit": "requests/s", "min": min(p[2] for p in points), "max": max(p[2] for p in points), "mean": round(sum(p[2] for p in points)/len(points), 3)}},
        "source": "reference.png", "reconstruction": "Approximate digitization; short latency peaks retained; RPS interpolated. Date replaced with requested date."
    }
    (ROOT / "metadata.json").write_text(json.dumps(metadata, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))

if __name__ == "__main__":
    main()
