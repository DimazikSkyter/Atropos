"""Conservative latency peak screening, using only Python's standard library.

Call analyze_modes separately for a stationary baseline or plateau window. Do
not pool the allocation ramp: changing latency levels can create apparent modes.
This is a histogram heuristic, not a test proving statistical bimodality.
"""

import math
import statistics


MIN_SAMPLES = 500
MIN_PEAK_MASS = 0.05
MIN_SEPARATION_MS = 4.0
MAX_VALLEY_RATIO = 0.65
MAX_LATENCY_MS = 60000.0


def _screen_peaks(samples, width_ms, offset_ms):
    """Find peaks in fixed, uniform bins; offset changes only bin alignment."""
    count = len(samples)
    bins = [0] * (int((max(samples) + offset_ms) / width_ms) + 3)
    for value in samples:
        bins[int((value + offset_ms) / width_ms)] += 1

    # Integer triangular smoothing. No random sampling or fitted mixture model.
    smooth = [
        (bins[i - 1] if i else 0) + 2 * bins[i]
        + (bins[i + 1] if i + 1 < len(bins) else 0)
        for i in range(len(bins))
    ]
    candidates = []
    index = 0
    while index < len(smooth):
        end = index
        while end + 1 < len(smooth) and smooth[end + 1] == smooth[index]:
            end += 1
        left = smooth[index - 1] if index else 0
        right = smooth[end + 1] if end + 1 < len(smooth) else 0
        if smooth[index] > left and smooth[index] > right:
            peak = (index + end) // 2
            mass = sum(bins[max(0, peak - 2):peak + 3]) / count
            if mass >= MIN_PEAK_MASS:
                candidates.append({
                    "index": peak,
                    "center": (peak + 0.5) * width_ms - offset_ms,
                    "height": smooth[peak],
                    "local_mass_fraction": mass,
                })
        index = end + 1

    # Prefer the strongest peak. An additional one must have both a substantial
    # local mass and a deep valley separating it from every retained peak.
    selected = []
    for candidate in sorted(candidates, key=lambda item: (-item["height"], item["index"])):
        distinct = True
        for other in selected:
            if abs(candidate["center"] - other["center"]) < MIN_SEPARATION_MS:
                distinct = False
                break
            lo, hi = sorted((candidate["index"], other["index"]))
            valley = min(smooth[lo + 1:hi])
            if valley > MAX_VALLEY_RATIO * min(candidate["height"], other["height"]):
                distinct = False
                break
        if distinct:
            selected.append(candidate)
    return sorted(selected, key=lambda item: item["center"])


def analyze_modes(values_ms):
    """Return conservative mode evidence for one approximately stationary window.

    Inputs are individual, nonnegative latency observations in milliseconds.
    The primary histogram has fixed 1 ms bins. A peak is accepted only if it
    persists with a half-bin shift and with two fixed 2 ms bin alignments.
    Each candidate needs >=5% of observations in its five-bin neighborhood,
    >=4 ms separation, and a separating valley <=65% of the lower peak.

    modes=0 means that this screen could not resolve a substantial stable peak;
    it does not mean that the underlying probability density has no mode.
    """
    samples = []
    discarded = 0
    for value in values_ms:
        number = float(value)
        if math.isfinite(number) and 0 <= number <= MAX_LATENCY_MS:
            samples.append(number)
        else:
            discarded += 1
    result = {
        "modes": 0,
        "centers_ms": [],
        "method": "fixed_uniform_histogram_peaks_with_alignment_and_resolution_checks",
        "samples": len(samples),
        "discarded_samples": discarded,
        "bucket_width_ms": 1.0,
        "minimum_peak_mass_fraction": MIN_PEAK_MASS,
        "minimum_separation_ms": MIN_SEPARATION_MS,
        "maximum_valley_ratio": MAX_VALLEY_RATIO,
        "limitations": [
            "A conservative peak heuristic, not a formal statistical proof of multimodality.",
            "Analyze baseline and plateau separately; pooling a changing ramp can create apparent modes.",
            "Nearby, broad, weak, or bin-sensitive modes may not be resolved by the fixed 1/2 ms grids.",
            "Quantized measurements can create peaks; inspect the histogram and raw timestamp resolution.",
        ],
    }
    if len(samples) < MIN_SAMPLES:
        result["status"] = "insufficient_samples"
        return result
    if discarded > 0.01 * (len(samples) + discarded):
        result["status"] = "insufficient_valid_coverage"
        result["limitations"].append(
            "More than 1% of observations are nonfinite, negative, or above 60000 ms."
        )
        return result

    primary = _screen_peaks(samples, 1.0, 0.0)
    checks = [
        _screen_peaks(samples, 1.0, 0.5),
        _screen_peaks(samples, 2.0, 0.0),
        _screen_peaks(samples, 2.0, 1.0),
    ]
    stable = [peak for peak in primary if all(
        # Strict radius keeps one sensitivity peak from validating two primary
        # peaks that are exactly 4 ms apart.
        any(abs(peak["center"] - other["center"]) < 2.0 for other in alternative)
        for alternative in checks
    )]
    # Report a center in measurement units, rather than a shifted bin center.
    centers = [statistics.median([
        value for value in samples if abs(value - peak["center"]) <= 2.5
    ]) for peak in stable]
    result.update({
        "modes": len(stable),
        "centers_ms": centers,
        "status": "resolved" if stable else "unresolved",
        "peak_local_mass_fractions": [peak["local_mass_fraction"] for peak in stable],
        "primary_candidate_centers_ms": [peak["center"] for peak in primary],
        "sensitivity_candidate_centers_ms": [
            [peak["center"] for peak in alternative] for alternative in checks
        ],
    })
    return result
