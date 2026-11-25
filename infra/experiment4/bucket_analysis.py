"""Compare actual Micrometer histogram distributions exported from VictoriaMetrics.

Uses cumulative Timer bucket deltas in separate baseline and plateau windows.
Raw request CSV and the pooled allocation ramp are never used here.
"""

import bisect
import json
import math
from pathlib import Path


PREFIX = "exp4_request_duration_seconds"
NAMES = {PREFIX + suffix for suffix in ("_bucket", "_count", "_sum", "_squared_sum")}
EXPECTED_FINITE = [.0005, .001, .002, .004] + [i / 1000 for i in range(5, 51)] + [.06, .075, .1, .15, .25, .5, 1.0]
MAX_GAP_MS = 6000.0
TV_THRESHOLD = .02


class _Invalid(ValueError):
    pass


def _point(points, timestamp):
    """Bounded interpolation; no extrapolation or interpolation through a gap."""
    times, values = points
    index = bisect.bisect_left(times, timestamp)
    if index < len(times) and times[index] == timestamp:
        return values[index]
    if index == 0 or index == len(times):
        raise _Invalid("A window endpoint is not bracketed by actual scrape samples")
    span = times[index] - times[index - 1]
    if span <= 0 or span > MAX_GAP_MS:
        raise _Invalid("Endpoint interpolation would cross a scrape gap above 6 seconds")
    weight = (timestamp - times[index - 1]) / span
    return values[index - 1] + weight * (values[index] - values[index - 1])


def _quantile(probabilities, quantile):
    cumulative = 0.0
    for interval in probabilities:
        mass = interval["fraction"]
        if mass > 0 and cumulative + mass >= quantile:
            if interval["upper_ms"] is None:
                return None  # A quantile in +Inf has no finite upper estimate.
            weight = (quantile - cumulative) / mass
            return interval["lower_ms"] + weight * (interval["upper_ms"] - interval["lower_ms"])
        cumulative += mass
    return None


def analyze_buckets(out):
    """Return validity, per-window moments/probabilities, and histogram TV distance.

    All moments come from Timer count/sum and the cumulative squared-duration
    gauge, using the same independently bracketed scrape endpoints. Interval
    probability masses come from differences of cumulative histogram deltas.
    The last interval has upper_ms=None, representing the +Inf overflow bucket.
    """
    out = Path(out)
    result = {
        "valid": False,
        "source": "shared_victoriametrics_export_of_actual_micrometer_timer",
        "method": "separate_window_cumulative_bucket_deltas_with_bounded_scrape_interpolation",
        "baseline_probabilities": [],
        "plateau_probabilities": [],
        "baseline": None,
        "plateau": None,
        "total_variation_distance": None,
        "distribution_changed_tv_0_02": False,
        "distribution_change_threshold": TV_THRESHOLD,
        "finite_bucket_boundaries": 0,
        "maximum_allowed_scrape_gap_seconds": MAX_GAP_MS / 1000,
        "errors": [],
        "limitations": [
            "Window endpoints are interpolated between actual scrapes, so counts may be fractional; no extrapolation is used.",
            "TV compares probability mass in the fixed Micrometer buckets; it does not prove bimodality or describe variation within a bucket.",
            "Histogram p99 uses uniform interpolation within its bucket; p99 in the +Inf interval is reported as null.",
            "Moment series are read during a scrape without a cross-meter atomic snapshot; inconsistent or negative variance is rejected.",
        ],
    }
    try:
        metadata = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        config = metadata["configuration"]
        baseline = float(config.get("BASELINE_SECONDS", 180))
        ramp = float(config.get("RAMP_SECONDS", 900))
        hold = float(config.get("HOLD_SECONDS", 180))
        epoch = float(metadata.get("start_time_unix_seconds", metadata.get("status", {}).get("start_time_unix_seconds", float("nan")))) * 1000
        if not all(math.isfinite(value) and value > 0 for value in (baseline, ramp, hold, epoch)):
            raise _Invalid("Missing or invalid phase durations or experiment start Unix timestamp")
        total = baseline + ramp + hold
        early = (min(60.0, baseline / 3), baseline)
        late = (baseline + ramp + max(0.0, hold - 120), total)
        required_start = epoch + early[0] * 1000
        required_end = epoch + late[1] * 1000
        raw = {}
        instances = set()
        for line in (out / "vm-export.ndjson").read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            metric = item.get("metric", {})
            name = metric.get("__name__")
            if name not in NAMES or any(metric.get(key) != value for key, value in {
                "job": "experiment4", "service": "java", "status": "200"
            }.items()) or metric.get("experiment", "experiment4") != "experiment4":
                continue
            instances.add(metric.get("instance", ""))
            if len(instances) > 1:
                raise _Invalid("Export mixes multiple Java instances; isolated-service histogram required")
            if name == PREFIX + "_bucket":
                boundary = float(metric["le"])
                if math.isnan(boundary) or boundary <= 0:
                    raise _Invalid("Invalid histogram bucket boundary")
                key = (name, boundary)
            else:
                key = (name, None)
            times, values = item.get("timestamps", []), item.get("values", [])
            if not times or len(times) != len(values):
                raise _Invalid("Missing or mismatched exported timestamps/values")
            merged = raw.setdefault(key, {})
            for timestamp, value in zip(times, values):
                timestamp, value = float(timestamp), float(value)
                if not math.isfinite(timestamp) or not math.isfinite(value) or value < 0:
                    raise _Invalid("Nonfinite or negative cumulative histogram/moment sample")
                if timestamp in merged and merged[timestamp] != value:
                    raise _Invalid("Conflicting duplicate exported scrape sample")
                merged[timestamp] = value

        boundaries = sorted(key[1] for key in raw if key[0] == PREFIX + "_bucket")
        finite = [boundary for boundary in boundaries if math.isfinite(boundary)]
        result["finite_bucket_boundaries"] = len(finite)
        if len(finite) != len(EXPECTED_FINITE) or any(
            abs(actual - expected) > 1e-12 for actual, expected in zip(finite, EXPECTED_FINITE)
        ) or boundaries[-1:] != [float("inf")]:
            raise _Invalid("Expected all 57 configured finite Micrometer boundaries plus +Inf")
        for suffix in ("_count", "_sum", "_squared_sum"):
            if (PREFIX + suffix, None) not in raw:
                raise _Invalid("Required Timer/moment series is missing: " + PREFIX + suffix)

        series = {}
        common_times = None
        for key, samples in raw.items():
            times = sorted(samples)
            left = bisect.bisect_right(times, required_start) - 1
            right = bisect.bisect_left(times, required_end)
            if left < 0 or right >= len(times):
                raise _Invalid("Required baseline/plateau endpoints are outside actual scrape coverage")
            times = times[left:right + 1]
            values = [samples[timestamp] for timestamp in times]
            if any(after - before > MAX_GAP_MS for before, after in zip(times, times[1:])):
                raise _Invalid("Scrape gap above 6 seconds within the baseline-to-plateau recording")
            if any(after < before for before, after in zip(values, values[1:])):
                raise _Invalid("Cumulative counter/moment reset within the analyzed recording")
            if common_times is None:
                common_times = times
            elif times != common_times:
                raise _Invalid("Histogram/moment series have different scrape timestamps")
            series[key] = (times, values)

        count_series = series[(PREFIX + "_count", None)][1]
        inf_series = series[(PREFIX + "_bucket", float("inf"))][1]
        if any(abs(count - inf) > 1e-8 for count, inf in zip(count_series, inf_series)):
            raise _Invalid("Timer count and +Inf bucket disagree at an actual scrape")
        for index in range(len(common_times)):
            cumulative = [series[(PREFIX + "_bucket", boundary)][1][index] for boundary in boundaries]
            if any(after < before for before, after in zip(cumulative, cumulative[1:])):
                raise _Invalid("Cumulative bucket ordering is invalid at an actual scrape")

        def window(begin, end):
            first, last = epoch + begin * 1000, epoch + end * 1000
            def delta(key):
                return _point(series[key], last) - _point(series[key], first)
            count = delta((PREFIX + "_count", None))
            if count <= 0:
                raise _Invalid("Window contains no recorded Timer observations")
            cumulative = [delta((PREFIX + "_bucket", boundary)) for boundary in boundaries]
            if abs(cumulative[-1] - count) > 1e-7:
                raise _Invalid("Window +Inf delta differs from Timer count delta")
            probabilities = []
            lower, previous = 0.0, 0.0
            for boundary, mass_to_boundary in zip(boundaries, cumulative):
                mass = mass_to_boundary - previous
                if mass < -1e-7:
                    raise _Invalid("Negative interval mass from cumulative bucket deltas")
                upper = boundary * 1000 if math.isfinite(boundary) else None
                probabilities.append({"lower_ms": lower, "upper_ms": upper,
                                      "fraction": max(0.0, mass) / count,
                                      "count": max(0.0, mass)})
                lower, previous = upper, mass_to_boundary
            if abs(sum(item["fraction"] for item in probabilities) - 1.0) > 1e-8:
                raise _Invalid("Histogram interval probabilities do not sum to one")
            mean_seconds = delta((PREFIX + "_sum", None)) / count
            variance_seconds = delta((PREFIX + "_squared_sum", None)) / count - mean_seconds ** 2
            if not math.isfinite(variance_seconds) or variance_seconds < -1e-12:
                raise _Invalid("Invalid variance from actual Timer/moment deltas")
            stats = {
                "begin_seconds": begin, "end_seconds": end, "count": count,
                "mean_ms": mean_seconds * 1000,
                "sigma_ms": math.sqrt(max(0.0, variance_seconds)) * 1000,
                "histogram_p99_ms": _quantile(probabilities, .99),
                "mass_over_10ms": 1 - cumulative[boundaries.index(.010)] / count,
                "mass_over_15ms": 1 - cumulative[boundaries.index(.015)] / count,
            }
            return stats, probabilities

        result["baseline"], result["baseline_probabilities"] = window(*early)
        result["plateau"], result["plateau_probabilities"] = window(*late)
        tv = sum(abs(a["fraction"] - b["fraction"]) for a, b in zip(
            result["baseline_probabilities"], result["plateau_probabilities"]
        )) / 2
        result.update(valid=True, total_variation_distance=tv,
                      distribution_changed_tv_0_02=tv >= TV_THRESHOLD,
                      analyzed_instance=next(iter(instances)),
                      actual_scrape_samples=len(common_times))
    except (OSError, ValueError, KeyError, TypeError) as error:
        result["errors"].append(str(error))
    return result
