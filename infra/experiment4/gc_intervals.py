"""Read Parallel GC pauses on the unified-log/request clock, without latency fitting.

OpenJDK GcInfo uses Management::timestamp(), whose epoch is VM initialization
completion. Its origin can differ from unified logging and RuntimeMXBean uptime.
The native GC log is therefore the authoritative overlap clock. The original
notification CSV is independently checked by generation and collection order.
"""

import csv
from datetime import datetime
import json
import math
from pathlib import Path
import re
import statistics


_LINE = re.compile(
    r"^\[(?P<wall>[^\]]+)\]\[(?P<uptime>[\d.]+)s\]"
    r"\[(?P<level>[^\]]+)\]\[(?P<tags>[^\]]+)\]\s*(?P<message>.*)$"
)
_PAUSE = re.compile(r"^GC\((\d+)\) Pause (Young|Full)\b")
_DURATION = re.compile(r"\b([\d.]+)ms\s*$")


def _stats(values):
    median = statistics.median(values) if values else None
    return {
        "count": len(values),
        "median_ms": median,
        "std_ms": statistics.pstdev(values) if len(values) > 1 else 0.0 if values else None,
        "maximum_absolute_residual_ms": max(
            (abs(value - median) for value in values), default=None
        ),
    }


def load_gc_intervals(out):
    """Return {events, clock_alignment} for one recorded JVM run.

    Event start/end are the separately logged Pause start and finish uptimes,
    in milliseconds. Event ms is the precise duration printed on the finish
    line; the log decorators have approximately 1 ms resolution. Missing,
    conflicting or inconsistent evidence is explicitly marked invalid.
    Nothing is inferred from request durations or their overlap with GC.
    """
    out = Path(out)
    errors = []
    files = sorted(path for path in out.glob("jvm-gc.log*") if path.is_file())
    starts, finishes = {}, {}
    origins = []
    origin_samples = []
    seen_lines = set()
    for file in files:
        for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line in seen_lines:
                continue
            seen_lines.add(line)
            match = _LINE.match(line)
            if not match:
                continue
            uptime = float(match["uptime"]) * 1000
            try:
                wall = datetime.fromisoformat(match["wall"].replace("Z", "+00:00"))
                if wall.tzinfo is None:
                    raise ValueError("Log wall time has no timezone")
                origin = wall.timestamp() * 1000 - uptime
                origins.append(origin)
                origin_samples.append((uptime, origin))
            except ValueError:
                errors.append("Invalid or timezone-free unified log wall timestamp")
            pause = _PAUSE.match(match["message"])
            if not pause:
                continue
            key = (int(pause[1]), "minor" if pause[2] == "Young" else "major")
            tags = {tag.strip() for tag in match["tags"].split(",")}
            if tags == {"gc", "start"}:
                target, record = starts, {"start": uptime}
            elif tags == {"gc"} and _DURATION.search(match["message"]):
                target, record = finishes, {
                    "end": uptime,
                    "ms": float(_DURATION.search(match["message"])[1]),
                }
            else:
                continue
            if key in target and target[key] != record:
                errors.append("Conflicting GC id/phase records; possible multiple JVM runs: " + str(key))
            else:
                target[key] = record

    events = []
    unmatched = sorted(set(starts) ^ set(finishes))
    if unmatched:
        errors.append("GC pause start/finish pairs are missing: " + str(unmatched[:10]))
    for key in sorted(set(starts) & set(finishes)):
        event = {"kind": key[1], "start": starts[key]["start"], **finishes[key], "gc_id": key[0]}
        span = event["end"] - event["start"]
        if span < 0 or event["ms"] < 0 or abs(span - event["ms"]) > 2.0:
            errors.append("GC start/finish decorator span differs from reported pause by >2 ms: " + str(key))
        events.append(event)
    events.sort(key=lambda event: event["start"])
    if not events:
        errors.append("No complete Parallel GC pause intervals in unified log")

    metadata_path = out / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    status = metadata.get("status", {})
    unix_start = metadata.get("start_time_unix_seconds", status.get("start_time_unix_seconds"))
    runtime_start = metadata.get("experiment_start_uptime_ms", status.get("experiment_start_uptime_ms"))
    # Unix wall clocks may be slewed by time synchronization. Check the runtime
    # anchor against the nearest log record to /start, rather than moving the
    # monotonic overlap clock to a whole-run median wall epoch.
    nearest_origin = min(origin_samples, key=lambda sample: abs(sample[0] - float(runtime_start))) \
        if origin_samples and runtime_start is not None else None
    log_origin = nearest_origin[1] if nearest_origin else None
    anchor_time_gap = abs(nearest_origin[0] - float(runtime_start)) if nearest_origin else None
    log_origin_stats = _stats(origins)
    runtime_origin = None
    anchor_delta = None
    if unix_start is not None and runtime_start is not None and log_origin is not None:
        runtime_origin = float(unix_start) * 1000 - float(runtime_start)
        anchor_delta = runtime_origin - log_origin
    runtime_valid = (
        anchor_delta is not None and math.isfinite(anchor_delta) and abs(anchor_delta) <= 5.0
        and anchor_time_gap is not None and anchor_time_gap <= 2000.0
    )
    if not runtime_valid:
        errors.append("Independent Unix/log-uptime versus RuntimeMXBean anchor validation failed or is missing")

    csv_path = out / "gc-events.csv"
    notifications = []
    if csv_path.exists():
        with csv_path.open(encoding="utf-8", newline="") as source:
            for row in csv.DictReader(source):
                item = {
                    "kind": row.get("kind", row.get("generation", "unknown")),
                    "start": float(row["start_ms"]), "end": float(row["end_ms"]),
                    "ms": float(row["duration_ms"]),
                }
                if item["kind"] not in ("minor", "major") or not all(
                    math.isfinite(item[name]) for name in ("start", "end", "ms")
                ) or item["start"] < 0 or item["end"] < item["start"] or item["ms"] < 0:
                    errors.append("Invalid GC notification CSV event")
                    continue
                notifications.append(item)
    else:
        errors.append("Original GC notification CSV is missing")

    offsets, start_offsets, end_offsets, duration_errors = [], [], [], []
    count_match = True
    by_kind = {}
    for kind in ("minor", "major"):
        native = [event for event in events if event["kind"] == kind]
        original = sorted((event for event in notifications if event["kind"] == kind), key=lambda event: event["start"])
        by_kind[kind] = {"unified_count": len(native), "notification_count": len(original)}
        if len(native) != len(original):
            count_match = False
            errors.append("Notification/unified collection counts differ for " + kind)
            continue
        for log, notification in zip(native, original):
            begin_offset = log["start"] - notification["start"]
            end_offset = log["end"] - notification["end"]
            start_offsets.append(begin_offset)
            end_offsets.append(end_offset)
            offsets.extend((begin_offset, end_offset))
            duration_errors.append(abs(log["ms"] - notification["ms"]))
    offset_stats = _stats(offsets)
    notification_valid = (
        count_match and len(start_offsets) >= 3
        and offset_stats["std_ms"] < 2.0
        and offset_stats["maximum_absolute_residual_ms"] <= 5.0
        and max(duration_errors, default=float("inf")) <= 3.0
    )
    if not notification_valid:
        errors.append("GC notification offset/order/duration validation failed or has fewer than three pairs")

    alignment = {
        "source": "unified_jvm_gc_log",
        "valid": not errors and runtime_valid and notification_valid,
        "runtime_anchor_valid": runtime_valid,
        "notification_validation_valid": notification_valid,
        "files": [file.name for file in files],
        "events": len(events),
        "unmatched_pause_pairs": len(unmatched),
        "timestamp_resolution_ms": 1.0,
        "event_interval": "GC(id) Pause start uptime to same GC(id) Pause finish uptime",
        "pause_duration": "Precise ms value printed on the corresponding unified finish line",
        "unix_anchor_delta_runtime_minus_unified_ms": anchor_delta,
        "unix_anchor_nearest_log_distance_ms": anchor_time_gap,
        "unix_anchor_method": "Metadata /start Unix/runtime epoch versus nearest independent unified ISO/uptime record (within 2 s)",
        "runtime_unix_origin_ms": runtime_origin,
        "unified_unix_origin_ms": log_origin,
        "unified_unix_origin_std_ms": log_origin_stats["std_ms"],
        "unified_unix_origin_max_residual_ms": log_origin_stats["maximum_absolute_residual_ms"],
        "unified_whole_run_unix_origin_range_ms": max(origins) - min(origins) if origins else None,
        "notification_to_unified_offset_ms": offset_stats["median_ms"],
        "notification_offset_std_ms": offset_stats["std_ms"],
        "notification_offset_max_residual_ms": offset_stats["maximum_absolute_residual_ms"],
        "notification_start_offsets": _stats(start_offsets),
        "notification_end_offsets": _stats(end_offsets),
        "maximum_notification_duration_error_ms": max(duration_errors, default=None),
        "counts_by_kind": by_kind,
        "errors": list(dict.fromkeys(errors)),
        "limitations": [
            "Unified uptime decorators resolve roughly 1 ms; GC overlap near either boundary is uncertain at that scale.",
            "Strict count/order validation rejects missing notifications or rotated-away log history rather than guessing matches.",
            "Clock alignment uses only GC records and independent Unix/runtime anchors, never request latency or GC-overlap outcomes.",
            "Whole-run Unix origin drift is reported separately: wall clock synchronization can slew Unix time without changing the monotonic overlap clock.",
        ],
        "openjdk_sources": [
            "https://github.com/openjdk/jdk21u/blob/master/src/hotspot/share/services/management.cpp",
            "https://github.com/openjdk/jdk/blob/master/src/hotspot/share/services/memoryManager.cpp",
        ],
    }
    return {"events": events, "clock_alignment": alignment}
