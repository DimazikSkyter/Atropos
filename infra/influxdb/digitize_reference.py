from pathlib import Path
import csv
import json

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
# Grid calibration: 14:00 at x=195, 16:00 at x=330; 0/40 ms at y=985/33.
X0, X1, Y0, Y40 = 98, 1700, 985, 33
rgb = np.asarray(Image.open(ROOT / "reference.png").convert("RGB"), dtype=float)
r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
masks = {
    "cassandra-1": (b > 75) & (b - r > 40) & (b > g * 1.23) & (g > r * 1.12),
    "cassandra-2": (r > 105) & (g > 55) & (g > r * .43) & (g < r * .77) & (g - b > 28) & (r - b > 70),
    "cassandra-3": (r > 100) & (r - g > 45) & (r - b > 45) & (np.abs(g - b) < 18),
    "cassandra-4": (r > 85) & (b > 75) & (r - g > 30) & (b - g > 30) & (r < b * 1.65) & (b < r * 1.65),
}
traces = {}
coverage = {}
excursions = {}
for host, mask in masks.items():
    excursions[host] = {}
    values = np.full(X1 - X0 + 1, np.nan)
    for index, x in enumerate(range(X0, X1 + 1)):
        ys = np.flatnonzero(mask[34:Y0 + 2, x]) + 34
        if host == "cassandra-3":
            # Orange/red JPEG fringes overlap at steep edges. Constrain the
            # rising red trace to the visible staircase until the lines meet.
            envelope = [(1250, 3), (1290, 4), (1310, 6), (1340, 9), (1350, 10.7),
                        (1398, 12.7), (1405, 15), (1420, 18), (1444, 21.7), (1460, 25.7)]
            ceiling = next((v for limit, v in envelope if x < limit), 40)
            if 502 <= x <= 508:
                ceiling = 22
            ys = ys[(Y0 - ys) * 40 / (Y0 - Y40) <= ceiling]
        if len(ys):
            # Favor horizontal stroke endpoints over the vertical connector.
            # This retains both upward peaks and brief downward excursions.
            support = mask[ys, max(X0, x-2):min(X1, x+2)+1].sum(axis=1)
            strength = rgb[ys, x].max(axis=1) - rgb[ys, x].min(axis=1)
            score = strength + support * 32
            chosen = ys[score >= score.max() - 4]
            values[index] = max(0, (Y0 - np.median(chosen)) * 40 / (Y0 - Y40))
            if host == "cassandra-3" and 502 <= x <= 508:
                values[index] = max(0, (Y0 - ys[0] - 1) * 40 / (Y0 - Y40))
            # In dense oscillation regions, the vertical stroke also preserves
            # the excursion endpoints. Place these at the next 15 s slots;
            # their exact subpixel timing is unavailable in a raster image.
            dense = ((host == "cassandra-1" and 1045 <= x <= 1173) or
                     (host == "cassandra-2" and 1385 <= x <= 1699) or
                     (host == "cassandra-3" and 1445 <= x <= 1699) or
                     (host == "cassandra-4" and 1365 <= x <= 1680))
            segments = np.split(ys, np.flatnonzero(np.diff(ys) > 1) + 1)
            stroke = max(segments, key=len)
            if dense and len(stroke) >= (10 if host == "cassandra-4" else 20):
                excursions[host][index] = tuple(max(0, (Y0-y) * 40/(Y0-Y40)) for y in (stroke[0]+1, stroke[-1]-1))
    valid = np.flatnonzero(~np.isnan(values))
    coverage[host] = {"observed_columns": len(valid), "total_columns": len(values)}
    # Occluded/indistinguishable strokes use the preceding observed value.
    # Exclude the isolated red peak from gap-fill anchors, so it cannot leak
    # into the following occluded baseline.
    anchors = valid
    if host == "cassandra-3":
        anchors = valid[~((valid + X0 >= 502) & (valid + X0 <= 509))]
    previous = np.clip(np.searchsorted(anchors, np.arange(len(values)), side="right") - 1, 0, len(anchors)-1)
    filled = values[anchors[previous]]
    values[np.isnan(values)] = filled[np.isnan(values)]
    values[:valid[0]] = values[valid[0]]
    for i in range(1, len(values)):
        if np.isnan(values[i]):
            values[i] = values[i - 1]
    # Remove raster/antialiasing drift around the clearly visible high plateaus.
    if host in ("cassandra-1", "cassandra-2", "cassandra-3"):
        levels = np.array([4, 5, 6, 7, 8.5, 10, 12, 14.5]) if host == "cassandra-1" else np.array([5, 6, 7, 8.5, 10, 12, 14.5, 17.5, 21, 25, 30, 36])
        high = np.flatnonzero(values >= (3.8 if host == "cassandra-1" else 4.8))
        values[high] = levels[np.abs(values[high, None] - levels).argmin(axis=1)]
    traces[host] = np.round(values, 3)

def calibrate(host, value):
    if host == "cassandra-1" and value >= 3.8:
        levels = [4, 5, 6, 7, 8.5, 10, 12, 14.5]
    elif host in ("cassandra-2", "cassandra-3") and value >= 4.8:
        levels = [5, 6, 7, 8.5, 10, 12, 14.5, 17.5, 21, 25, 30, 36]
    else:
        return round(value, 3)
    return min(levels, key=lambda level: abs(level-value))

points = []
for i in range(X1-X0+1):
    elapsed = i * 86400/(X1-X0)
    values = {host: float(traces[host][i]) for host in traces}
    points.append((elapsed, values))
    if any(i in excursions[host] for host in traces):
        for slot in (0, 1):
            additional = {host: calibrate(host, excursions[host][i][slot]) if i in excursions[host] else values[host] for host in traces}
            points.append((elapsed + (slot+1)*15, additional))

with (ROOT / "reference-traces.csv").open("w", newline="", encoding="utf-8") as out:
    writer = csv.writer(out)
    writer.writerow(["elapsed_seconds", *traces])
    for elapsed, values in points:
        writer.writerow([round(elapsed, 3), *(values[h] for h in traces)])
print(json.dumps(coverage, indent=2))

# Local QA figure only; matplotlib is not required by the replay stack.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MultipleLocator

colors = ["#1f78c1", "#ef843c", "#e24d42", "#ba43a9"]
plt.rcParams.update({"text.color": "#d8d9da", "axes.labelcolor": "#d8d9da", "xtick.color": "#d8d9da", "ytick.color": "#d8d9da"})
fig, ax = plt.subplots(figsize=(17.4, 10.54), dpi=100)
fig.patch.set_facecolor("#171d20")
ax.set_facecolor("#171d20")
hours = [12.5 + elapsed/3600 for elapsed, _ in points]
for host, color in zip(traces, colors):
    ax.step(hours, [values[host] for _, values in points], where="post", label=host, color=color, linewidth=1.15)
ax.set(xlim=(12.5, 36.5), ylim=(0, 40), ylabel="ms", title="cassandra_response_time · 01.10.2026 12:30 — 02.10.2026 12:30 (MSK)")
ax.xaxis.set_major_locator(MultipleLocator(2))
ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{int(value) % 24:02d}:00"))
ax.yaxis.set_major_locator(MultipleLocator(5))
ax.grid(color="#879096", alpha=.25)
ax.legend(loc="upper left", frameon=False, ncols=4)
for spine in ax.spines.values():
    spine.set_edgecolor("#6b7075")
fig.tight_layout()
fig.savefig(ROOT / "preview.png")
