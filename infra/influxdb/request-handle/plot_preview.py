"""Optional static QA preview; requires matplotlib."""
from pathlib import Path
from datetime import datetime
import csv
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

ROOT = Path(__file__).resolve().parent
rows = list(csv.DictReader((ROOT / "metrics.csv").open(encoding="utf-8", newline="")))
times = [datetime.fromisoformat(row["timestamp"]) for row in rows]
calibration = json.loads((ROOT / "calibration.json").read_text())
plt.rcParams.update({"text.color": "#d8d9da", "axes.labelcolor": "#d8d9da", "xtick.color": "#d8d9da", "ytick.color": "#d8d9da"})
fig, ax = plt.subplots(figsize=(17.51, 10.58), dpi=100)
fig.patch.set_facecolor("#171d20")
ax.set_facecolor("#171d20")
right = ax.twinx()
green = right.plot(times, [float(row["total_rps"]) for row in rows], color="#73BF69", linewidth=1, label="total_rps")
yellow = ax.plot(times, [float(row["request_handle_time_ms"]) for row in rows], color="#E0B400", linewidth=1, label="request_handle_time (200)")
ax.set(xlim=(times[0], times[-1]), ylim=(0, calibration["left_axis_max_ms"]), ylabel="ms", title="InfluxDB Replay · 03.10.2026 · request_handle_time / total_rps")
right.set(ylim=(0, calibration["right_axis_max_rps"]), ylabel="RPS")
ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0, 15, 30, 45], tz=times[0].tzinfo))
ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=times[0].tzinfo))
ax.grid(color="#879096", alpha=.18)
ax.legend(yellow+green, [p.get_label() for p in yellow+green], loc="lower right", frameon=False)
for axis in (ax, right):
    for spine in axis.spines.values():
        spine.set_edgecolor("#6b7075")
fig.tight_layout()
fig.savefig(ROOT / "preview.png")
