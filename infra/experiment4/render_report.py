"""Plot actual Micrometer histogram counters exported from VictoriaMetrics.

Usage: python render_report.py results/<UTCstamp> [--output report.png]
Requires numpy and matplotlib. No request/GC CSV is read by this report.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


REQUEST_PREFIX = "exp4_request_duration_seconds"


@dataclass
class Series:
    labels: dict[str, str]
    timestamps: np.ndarray
    values: np.ndarray


def read_export(path: Path, instance: str | None = None) -> list[Series]:
    """Merge VM export chunks by the complete label set, then deduplicate timestamps."""
    groups: dict[tuple, dict[int, float]] = {}
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            labels = record.get("metric", {})
            if not str(labels.get("__name__", "")).startswith("exp4_"):
                continue
            if instance and labels.get("instance") != instance:
                continue
            timestamps, values = record.get("timestamps", []), record.get("values", [])
            if len(timestamps) != len(values):
                raise ValueError(f"VM export line {line_number}: timestamps/values length mismatch")
            key = tuple(sorted(labels.items()))
            samples = groups.setdefault(key, {})
            for timestamp, value in zip(timestamps, values):
                samples[int(timestamp)] = float(value)
    result = []
    for key, samples in groups.items():
        ordered = sorted(samples)
        result.append(Series(dict(key), np.array(ordered, dtype=np.int64),
                             np.array([samples[t] for t in ordered], dtype=float)))
    return result


def select(series: list[Series], name: str, required: bool = True, **labels) -> Series | None:
    matches = [item for item in series if item.labels.get("__name__") == name
               and item.labels.get("service") == "java"
               and all(item.labels.get(key) == value for key, value in labels.items())]
    if len(matches) > 1:
        instances = sorted({item.labels.get("instance", "<unlabelled>") for item in matches})
        raise ValueError(f"Multiple series for {name} {labels}: {instances}; select --instance")
    if not matches:
        if required:
            raise ValueError(f"No Micrometer series {name} with service=java and {labels}")
        return None
    return matches[0]


@dataclass
class Counters:
    times: np.ndarray                 # Unix seconds, common scrape timestamps.
    values: np.ndarray                # columns: finite buckets, count, sum, squared sum.
    bounds: np.ndarray                # seconds, finite upper boundaries.
    invalid_intervals: np.ndarray     # interval i spans times[i] .. times[i+1].
    gap_intervals: np.ndarray
    reset_intervals: np.ndarray
    overflow_intervals: np.ndarray
    gc: dict[str, tuple[np.ndarray, np.ndarray]]
    excluded_scrape_timestamps: int


def align_counters(series: list[Series], max_gap: float) -> Counters:
    bucket_series = []
    for item in series:
        if (item.labels.get("__name__") == REQUEST_PREFIX + "_bucket"
                and item.labels.get("service") == "java" and item.labels.get("status") == "200"):
            boundary = float(item.labels["le"])
            if math.isfinite(boundary):
                bucket_series.append((boundary, item))
    bucket_series.sort(key=lambda pair: pair[0])
    if not bucket_series or len({bound for bound, _ in bucket_series}) != len(bucket_series):
        raise ValueError("No unique finite Micrometer buckets; check export or select --instance")
    count = select(series, REQUEST_PREFIX + "_count", status="200")
    total = select(series, REQUEST_PREFIX + "_sum", status="200")
    squared = select(series, REQUEST_PREFIX + "_squared_sum", status="200")
    mandatory = [item for _, item in bucket_series] + [count, total, squared]
    common = mandatory[0].timestamps
    for item in mandatory[1:]:
        common = np.intersect1d(common, item.timestamps, assume_unique=True)
    if len(common) < 2:
        raise ValueError("Fewer than two common histogram/count/moment scrapes in VM export")
    matrix = np.column_stack([item.values[np.searchsorted(item.timestamps, common)]
                              for item in mandatory])
    times = common.astype(float) / 1000
    delta = np.diff(matrix, axis=0)
    dt = np.diff(times)
    gaps = dt > max_gap
    resets = np.any(delta < -1e-9, axis=1)
    nonfinite = ~np.all(np.isfinite(matrix[:-1]) & np.isfinite(matrix[1:]), axis=1)
    # Each *increment* of a cumulative histogram must remain cumulative in le.
    finite_delta = delta[:, :len(bucket_series)]
    interval_delta = np.diff(np.column_stack([np.zeros(len(delta)), finite_delta]), axis=1)
    total_delta = delta[:, len(bucket_series)]
    inconsistent = (np.any(interval_delta < -1e-7, axis=1)
                    | (finite_delta[:, -1] > total_delta + 1e-7))
    invalid = gaps | resets | nonfinite | inconsistent | (dt <= 0)
    gc = {}
    for generation in ("minor", "major"):
        collections = select(series, "exp4_gc_collections_total", required=False, generation=generation)
        pauses = select(series, "exp4_gc_pause_seconds_total", required=False, generation=generation)
        if collections is not None and pauses is not None:
            gc[generation] = (collections.timestamps.astype(float) / 1000, collections.values)
            gc[generation + "_pause"] = (pauses.timestamps.astype(float) / 1000, pauses.values)
    return Counters(times, matrix, np.array([bound for bound, _ in bucket_series]), invalid,
                    gaps, resets, inconsistent, gc, len(count.timestamps) - len(common))


def counter_delta(times: np.ndarray, values: np.ndarray, begin: float, end: float,
                  max_gap: float) -> np.ndarray | None:
    """Exact-window delta using linear interpolation inside valid scrape intervals only."""
    if begin < times[0] or end > times[-1] or end <= begin:
        return None
    left = max(0, int(np.searchsorted(times, begin, side="right")) - 1)
    right = min(len(times) - 2, int(np.searchsorted(times, end, side="left")) - 1)
    if right < left:
        return None
    segment = values[left:right + 2]
    if (np.any(np.diff(times[left:right + 2]) > max_gap)
            or not np.all(np.isfinite(segment)) or np.any(np.diff(segment, axis=0) < -1e-9)):
        return None
    def at(when):
        index = int(np.searchsorted(times, when, side="left"))
        if index < len(times) and times[index] == when:
            return values[index]
        if index == 0 or index == len(times):
            raise ValueError("Counter interpolation would extrapolate")
        fraction = (when - times[index - 1]) / (times[index] - times[index - 1])
        return values[index - 1] + fraction * (values[index] - values[index - 1])
    return at(end) - at(begin)


def moments(delta: np.ndarray, bucket_count: int) -> tuple[float, float]:
    count, total, squared = delta[bucket_count:bucket_count + 3]
    if count <= 0:
        return math.nan, math.nan
    mean = total / count
    variance = max(0, squared / count - mean * mean)
    return mean * 1000, math.sqrt(variance) * 1000


def window_delta(data: Counters, begin: float, end: float, max_gap: float) -> np.ndarray | None:
    overlap = (data.times[:-1] < end) & (data.times[1:] > begin)
    if np.any(data.invalid_intervals & overlap):
        return None
    return counter_delta(data.times, data.values, begin, end, max_gap)


def aggregate_period(data: Counters, begin: float, end: float) -> dict:
    """Sum actual valid scrape deltas, weighting only the interval overlapping a phase."""
    elapsed = np.diff(data.times)
    overlaps = np.maximum(0, np.minimum(data.times[1:], end) - np.maximum(data.times[:-1], begin))
    valid = ~data.invalid_intervals & (overlaps > 0)
    weights = np.where(valid, overlaps / elapsed, 0)
    delta = np.sum(np.where(valid[:, None], np.diff(data.values, axis=0), 0) * weights[:, None], axis=0)
    cumulative = delta[:len(data.bounds)]
    counts = np.maximum(0, np.diff(np.r_[0, cumulative]))
    count = float(delta[len(data.bounds)])
    mean_ms, sigma_ms = moments(delta, len(data.bounds))
    return {"requested_begin_unix_seconds": begin, "requested_end_unix_seconds": end,
            "observed_seconds": float(np.sum(overlaps[valid])), "count": count,
            "mean_ms": mean_ms, "sigma_ms": sigma_ms,
            "interval_counts": counts.tolist(),
            "overflow_above_last_finite_bucket": max(0, count - float(cumulative[-1]))}


def draw_report(result_dir: Path, output: Path, summary_path: Path | None,
                window_seconds: float, step_seconds: float, max_gap: float,
                instance: str | None, max_latency_ms: float = 50) -> dict:
    source = result_dir / "vm-export.ndjson"
    metadata = json.loads((result_dir / "metadata.json").read_text(encoding="utf-8-sig"))
    series = read_export(source, instance)
    data = align_counters(series, max_gap)
    if not np.any(data.bounds * 1000 <= max_latency_ms):
        raise ValueError("--max-latency-ms is below all exported Micrometer bucket bounds")
    config = metadata.get("configuration", {})
    baseline = float(config.get("BASELINE_SECONDS", 180))
    ramp = float(config.get("RAMP_SECONDS", 900))
    hold = float(config.get("HOLD_SECONDS", 180))
    origin = float(metadata.get("start_time_unix_seconds", data.times[0]))
    planned_end = origin + baseline + ramp + hold
    available_end = min(data.times[-1], planned_end)
    first_end = origin + math.ceil((max(origin, data.times[0]) + window_seconds - origin) / step_seconds) * step_seconds
    ends = np.arange(first_end, available_end + 1e-6, step_seconds)
    if len(ends) == 0:
        raise ValueError(f"Not enough common VM samples for a {window_seconds:g}s histogram window")
    nb = len(data.bounds)
    histogram = np.full((len(ends), nb), np.nan)
    mean_ms = np.full(len(ends), np.nan)
    sigma_ms = np.full(len(ends), np.nan)
    rps = np.full(len(ends), np.nan)
    gc_rates = {name: np.full(len(ends), np.nan) for name in data.gc}
    valid_windows = np.zeros(len(ends), dtype=bool)
    for index, end in enumerate(ends):
        begin = end - window_seconds
        delta = window_delta(data, begin, end, max_gap)
        if delta is not None and delta[nb] > 0:
            bins = np.diff(np.r_[0, delta[:nb]])
            if np.all(bins >= -1e-7):
                histogram[index] = np.maximum(0, bins) / window_seconds
                mean_ms[index], sigma_ms[index] = moments(delta, nb)
                rps[index] = delta[nb] / window_seconds
                valid_windows[index] = True
        for name, (gc_times, gc_values) in data.gc.items():
            gc_delta = counter_delta(gc_times, gc_values, begin, end, max_gap)
            if gc_delta is not None:
                gc_rates[name][index] = float(gc_delta) / window_seconds
    if not np.any(valid_windows):
        raise ValueError("No valid windows: histogram resets, missing scrapes, or no completed requests")
    warmup = min(60, baseline / 3)
    base_period = aggregate_period(data, origin + warmup, origin + baseline)
    hold_begin = origin + baseline + ramp
    late_begin = max(hold_begin, planned_end - 120)
    late_period = aggregate_period(data, late_begin, planned_end)
    gap_records = [{"begin_seconds": float(a - origin), "end_seconds": float(b - origin),
                    "duration_seconds": float(b - a)}
                   for a, b in zip(data.times[:-1][data.gap_intervals], data.times[1:][data.gap_intervals])]
    reset_records = [{"begin_seconds": float(a - origin), "end_seconds": float(b - origin)}
                     for a, b in zip(data.times[:-1][data.reset_intervals], data.times[1:][data.reset_intervals])]
    summary = {"source": "VictoriaMetrics export of actual Micrometer Timer cumulative buckets",
               "vm_export_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
               "window_seconds": window_seconds, "step_seconds": step_seconds, "max_gap_seconds": max_gap,
               "max_displayed_latency_ms": max_latency_ms,
               "color_normalization": {"type": "power", "gamma": .35},
               "baseline_warmup_excluded_seconds": warmup,
               "common_scrapes": len(data.times), "excluded_scrape_timestamps": data.excluded_scrape_timestamps,
               "finite_bucket_bounds_ms": (data.bounds * 1000).tolist(),
               "valid_windows": int(valid_windows.sum()), "invalid_windows": int((~valid_windows).sum()),
               "gaps": gap_records, "counter_resets": reset_records,
               "inconsistent_histogram_intervals": int(data.overflow_intervals.sum()),
               "baseline": base_period, "last_hold": late_period,
               "last_hold_requested_seconds": planned_end - late_begin,
               "figure": str(output.resolve())}
    plot_report(metadata, data, ends, origin, histogram, mean_ms, sigma_ms, rps,
                gc_rates, baseline, ramp, hold, base_period, late_period, summary, output)
    if summary_path:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(clean_json(summary), indent=2, allow_nan=False), encoding="utf-8")
    return clean_json(summary)


def clean_json(value):
    if isinstance(value, dict):
        return {key: clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def plot_report(metadata, data, ends, origin, histogram, mean_ms, sigma_ms, rps,
                gc_rates, baseline, ramp, hold, base_period, late_period, summary, output):
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.titlesize": 12, "axes.labelsize": 10})
    figure = plt.figure(figsize=(16, 12), layout="constrained")
    grid = figure.add_gridspec(3, 2, height_ratios=[1.55, 1, 1])
    heat = figure.add_subplot(grid[0, :])
    gc_axis = figure.add_subplot(grid[1, 0])
    moments_axis = figure.add_subplot(grid[1, 1])
    shape_axis = figure.add_subplot(grid[2, 0])
    rps_axis = figure.add_subplot(grid[2, 1])
    profile_kind = "CONTROL" if metadata.get("control") else "RAMP"
    run_kind = "PILOT CALIBRATION" if metadata.get("kind") == "pilot" else "FULL EXPERIMENT"
    figure.suptitle(f"Experiment4 | {run_kind} | {profile_kind} | Micrometer histogram and minor GC\n"
                   f"{metadata.get('created_utc', '')} | {summary['window_seconds']:g}s windows | "
                   f"{summary['valid_windows']} valid / {summary['invalid_windows']} excluded windows", fontsize=15)
    x = (ends - origin - summary["window_seconds"] / 2) / 60
    dx = summary["step_seconds"] / 60
    x_edges = np.r_[x - dx / 2, x[-1] + dx / 2]
    shown = data.bounds * 1000 <= summary["max_displayed_latency_ms"] + 1e-9
    y_edges = np.r_[0, data.bounds[shown] * 1000]
    colors = plt.colormaps["inferno"].copy()
    colors.set_bad("#dadada")
    from matplotlib.colors import PowerNorm
    mesh = heat.pcolormesh(x_edges, y_edges, np.ma.masked_invalid(histogram[:, shown].T),
                           shading="flat", cmap=colors,
                           norm=PowerNorm(gamma=summary["color_normalization"]["gamma"], vmin=0))
    figure.colorbar(mesh, ax=heat, label="Requests/s per bucket interval (color: power gamma=0.35)")
    heat.set(title="Distribution over time: finite Micrometer SLO bucket increments",
             xlabel="Minutes since POST /start (window center)", ylabel="Bucket bounds, ms",
             ylim=(0, summary["max_displayed_latency_ms"]))
    gc_axis.plot(x, gc_rates.get("minor", np.full(len(x), np.nan)), color="#1768ac", label="minor collections/s")
    if "major" in gc_rates:
        gc_axis.plot(x, gc_rates["major"], color="#ba2d0b", label="major collections/s", linewidth=1)
    pause_axis = gc_axis.twinx()
    pause_axis.plot(x, gc_rates.get("minor_pause", np.full(len(x), np.nan)) * 100,
                    color="#ee964b", label="minor pause time %", linewidth=1.5)
    gc_axis.set(title="Real JVM GC counters from VM", ylabel="Collections/s", xlabel="Minutes since /start", ylim=(0, None))
    pause_axis.set(ylabel="Minor GC time, % of window", ylim=(0, None))
    handles, labels = gc_axis.get_legend_handles_labels()
    p_handles, p_labels = pause_axis.get_legend_handles_labels()
    gc_axis.legend(handles + p_handles, labels + p_labels, loc="upper left", fontsize=9)
    moments_axis.plot(x, sigma_ms, color="#ce4257", label="sigma")
    moments_axis.plot(x, mean_ms, color="#1768ac", label="mean")
    moments_axis.set(title="Individual-observation moments (Micrometer counters)",
                     ylabel="Response time, ms", xlabel="Minutes since /start", ylim=(0, None))
    moments_axis.legend(loc="upper left")
    widths = np.diff(np.r_[0, data.bounds * 1000])
    for period, color, label in [(base_period, "#1768ac", "Warmed baseline"),
                                  (late_period, "#ce4257", f"Last {summary['last_hold_requested_seconds']:g}s of hold")]:
        if period["count"] > 0:
            density = np.array(period["interval_counts"]) / period["count"] / widths * 100
            shape_axis.stairs(density[shown], y_edges, color=color, linewidth=1.8,
                              label=f"{label}: n={period['count']:,.0f}, sigma={period['sigma_ms']:.2f}ms")
    shape_axis.set_yscale("symlog", linthresh=.05)
    shape_axis.set(title="Phase distributions from cumulative Micrometer buckets",
                   xlim=(0, summary["max_displayed_latency_ms"]),
                   xlabel="Response time bucket, ms", ylabel="Probability density, % / ms (symlog)", ylim=(0, None))
    shape_axis.grid(alpha=.2)
    shape_axis.legend(loc="upper right", fontsize=9)
    rps_axis.plot(x, rps, color="#208b3a", label="Completed /work requests/s")
    offered = float(metadata.get("load_rps", 250))
    rps_axis.axhline(offered, color="#555555", linestyle="--", linewidth=1, label=f"Configured offered {offered:g} RPS")
    rps_axis.set(title="Completed request rate; source integrity", ylabel="Requests/s", xlabel="Minutes since /start", ylim=(0, None))
    rps_axis.legend(loc="upper left", fontsize=9)
    rps_axis.text(.01, .04,
                  f"Common scrapes: {summary['common_scrapes']}\n"
                  f"Gaps > {summary['max_gap_seconds']:g}s: {len(summary['gaps'])}\n"
                  f"Counter reset intervals: {len(summary['counter_resets'])}\n"
                  f"Missing from aligned scrapes: {summary['excluded_scrape_timestamps']}\n"
                  f"Baseline warmup excluded: {summary['baseline_warmup_excluded_seconds']:.2f}s\n"
                  f"Baseline coverage: {base_period['observed_seconds']:.1f}s; hold coverage: {late_period['observed_seconds']:.1f}s",
                  transform=rps_axis.transAxes, fontsize=9, va="bottom",
                  bbox={"facecolor": "white", "alpha": .85, "edgecolor": "#cccccc"})
    for axis in [heat, gc_axis, moments_axis, rps_axis]:
        axis.axvline(baseline / 60, color="#62a87c", linestyle="--", linewidth=1)
        axis.axvline((baseline + ramp) / 60, color="#62a87c", linestyle="--", linewidth=1)
        axis.set_xlim(0, (baseline + ramp + hold) / 60)
        if axis is not heat:
            axis.grid(alpha=.2)
        for gap in summary["gaps"]:
            axis.axvspan(gap["begin_seconds"] / 60, gap["end_seconds"] / 60,
                         facecolor="#888888", alpha=.25, hatch="//", edgecolor="#555555")
        for reset in summary["counter_resets"]:
            axis.axvspan(reset["begin_seconds"] / 60, reset["end_seconds"] / 60,
                         facecolor="#ce4257", alpha=.2)
    phase_style = {"facecolor": "white", "alpha": .8, "edgecolor": "none", "pad": 2}
    heat.text(baseline / 120, .98, "BASELINE", transform=heat.get_xaxis_transform(), ha="center", va="top", color="#222222", fontsize=10, bbox=phase_style)
    heat.text((baseline + ramp / 2) / 60, .98, "ALLOCATION RAMP", transform=heat.get_xaxis_transform(), ha="center", va="top", color="#222222", fontsize=10, bbox=phase_style)
    heat.text((baseline + ramp + hold / 2) / 60, .98, "HOLD", transform=heat.get_xaxis_transform(), ha="center", va="top", color="#222222", fontsize=10, bbox=phase_style)
    if summary["gaps"]:
        heat.text(.99, .05, "Hatched spans: scrape gaps; grey cells: excluded windows",
                  transform=heat.transAxes, ha="right", color="#222222", fontsize=9,
                  bbox={"facecolor": "white", "alpha": .85, "edgecolor": "none"})
    figure.supxlabel("Source: actual Micrometer counters exported from VM. Scrape deltas exclude resets/gaps. "
                     "Phase density = interval probability / bucket width; symlog reveals low-weight buckets.", fontsize=8)
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160, facecolor="white")
    plt.close(figure)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dir", type=Path)
    parser.add_argument("--output", type=Path, help="Default: <result_dir>/report.png")
    parser.add_argument("--summary", type=Path, help="Optional JSON summary output path")
    parser.add_argument("--window-seconds", type=float, default=60)
    parser.add_argument("--step-seconds", type=float, default=10)
    parser.add_argument("--max-gap-seconds", type=float, default=6)
    parser.add_argument("--max-latency-ms", type=float, default=50,
                        help="Maximum latency shown in heatmap and phase curves (default: 50 ms)")
    parser.add_argument("--instance", help="Filter one VM instance if multiple runs/targets occur in the export")
    args = parser.parse_args()
    numbers = [args.window_seconds, args.step_seconds, args.max_gap_seconds, args.max_latency_ms]
    if not all(math.isfinite(value) and value > 0 for value in numbers):
        parser.error("Window, step, gap seconds, and max latency must be finite positive numbers")
    try:
        summary = draw_report(args.result_dir, args.output or args.result_dir / "report.png",
                              args.summary, args.window_seconds, args.step_seconds,
                              args.max_gap_seconds, args.instance, args.max_latency_ms)
    except (OSError, ValueError, KeyError) as error:
        print(f"Report failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
