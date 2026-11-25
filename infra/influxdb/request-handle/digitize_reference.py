"""Digitize the two reference traces. Optional: numpy, Pillow, matplotlib."""
from pathlib import Path
import csv
import json

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
rgb = np.asarray(Image.open(ROOT / "reference.png").convert("RGB"), dtype=float)
r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
WIDTH, PLOT_TOP, PLOT_BOTTOM = rgb.shape[1], 20, 973
yellow = (r > 75) & (g > 60) & (r - b > 38) & (g - b > 35) & (r > g * .85) & (r < g * 1.65)
green = (g > 65) & (g - r > 20) & (g - b > 20)

def trace(mask, low, high, upper=False):
    result = np.full(WIDTH, np.nan)
    observed = 0
    for x in range(WIDTH):
        ys = np.flatnonzero(mask[low:high, x]) + low
        if len(ys):
            chroma = rgb[ys, x].max(axis=1) - rgb[ys, x].min(axis=1)
            support = mask[ys, max(0, x-1):min(WIDTH, x+2)].sum(axis=1)
            score = chroma + support * 12
            selected = ys[ys <= ys[0]+2] if upper else ys[score >= score.max() - 4]
            result[x] = np.median(selected)
            observed += 1
    valid = np.flatnonzero(np.isfinite(result))
    result = np.interp(np.arange(WIDTH), valid, result[valid])
    return result, observed

# Exclude tooltip/legend (below y=740), and separate the two color bands.
yellow_y, yellow_observed = trace(yellow, 480, 741, upper=True)
green_y, green_observed = trace(green, 85, 225)
# Tooltip gives 13.5 ms at the highlighted point. The user gives ~10k RPS.
# The hover marker is wider than the trace; use its center for calibration.
anchor_center, _ = trace(yellow, 480, 741)
anchor_y = float(np.median(anchor_center[85:92]))
ms_per_pixel = 13.5 / (PLOT_BOTTOM-anchor_y)
green_mean_y = float(np.mean(green_y))
rps_per_pixel = 10000 / (PLOT_BOTTOM-green_mean_y)
response = (PLOT_BOTTOM-yellow_y) * ms_per_pixel
rps = (PLOT_BOTTOM-green_y) * rps_per_pixel
with (ROOT / "reference-traces.csv").open("w", encoding="utf-8", newline="") as out:
    writer = csv.writer(out)
    writer.writerow(["elapsed_seconds", "request_handle_time_ms", "total_rps"])
    for x in range(WIDTH):
        writer.writerow([round(x*14040/(WIDTH-1), 6), round(float(response[x]), 3), round(float(rps[x]), 3)])
calibration = {
    "plot_top": PLOT_TOP, "plot_bottom": PLOT_BOTTOM, "plot_width": WIDTH,
    "yellow_tooltip_ms": 13.5, "yellow_anchor_y": anchor_y,
    "ms_per_pixel": ms_per_pixel, "green_mean_y": green_mean_y,
    "rps_per_pixel": rps_per_pixel, "yellow_observed_columns": yellow_observed,
    "green_observed_columns": green_observed,
    "left_axis_max_ms": round((PLOT_BOTTOM-PLOT_TOP)*ms_per_pixel, 3),
    "right_axis_max_rps": round((PLOT_BOTTOM-PLOT_TOP)*rps_per_pixel, 3),
    "limitations": "Axes are cropped. Yellow scale anchored to the 13.5 ms tooltip; green scale anchored to user-provided ~10000 RPS. Raster values and subpixel timing are approximate."
}
(ROOT / "calibration.json").write_text(json.dumps(calibration, indent=2)+"\n", encoding="utf-8")
print(json.dumps(calibration, indent=2))
