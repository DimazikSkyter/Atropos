"""Extract the visible yellow polyline; no random samples or invented excursions."""
import csv
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
X0, X1 = 20, 1207
Y220, Y740 = 571, 16
DURATION = 380 * 60


def main():
    original = Image.open(ROOT / "reference.png").convert("RGB")
    rgb = np.asarray(original, dtype=float)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mask = (r > 80) & (g > 65) & (g-b > 30) & (r > g*.95) & (r < g*1.8)
    upper, lower, center = [], [], []
    for x in range(X0, X1+1):
        ys = np.flatnonzero(mask[7:572, x]) + 7
        if not len(ys):
            raise ValueError(f"No yellow stroke at x={x}")
        upper.append(float(ys[0]))
        lower.append(float(ys[-1]))
        strength = g[ys,x] - b[ys,x]
        center.append(float(np.median(ys[strength >= strength.max()-2])))

    # The two raster envelopes reveal the sharp tips of the zigzag. A median
    # per column alone would flatten those peaks into false smooth data.
    candidates = []
    for i in range(1, len(upper)-1):
        hi, lo = upper[i], lower[i]
        is_peak = hi <= min(upper[i-1],upper[i+1]) and hi < max(upper[i-1],upper[i+1])-1
        is_trough = lo >= max(lower[i-1],lower[i+1]) and lo > min(lower[i-1],lower[i+1])+1
        if is_peak:
            candidates.append((X0+i, hi+0.5, "peak"))
        if is_trough:
            candidates.append((X0+i, lo-0.5, "trough"))
    # Merge consecutive extrema of the same kind, keeping the outermost tip.
    tips = []
    for item in candidates:
        if tips and tips[-1][2] == item[2]:
            if (item[2] == "peak" and item[1] < tips[-1][1]) or (item[2] == "trough" and item[1] > tips[-1][1]):
                tips[-1] = item
        else:
            tips.append(item)
    knots = [(X0,center[0],"edge"), *tips, (X1,center[-1],"edge")]
    snapped = {}
    for x,y,kind in knots:
        second = round((x-X0)*DURATION/(X1-X0)/15)*15
        if second in snapped:
            # Ambiguous subpixel extrema have no recoverable exact ordering;
            # retain the larger visible deviation from the central 480ms band.
            if abs(y-294) <= abs(snapped[second][0]-294):
                continue
        snapped[second] = (y,kind,x)
    # Preserve the low-amplitude initial stroke and any long runs without tips.
    anchors = sorted(snapped)
    for i,x in enumerate(range(X0,X1+1)):
        second = round((x-X0)*DURATION/(X1-X0)/15)*15
        if second in snapped:
            continue
        if min(abs(second-a) for a in anchors) >= 60:
            snapped[second] = (center[i],"stroke",x)
    with (ROOT / "reference-knots.csv").open("w",encoding="utf-8",newline="") as stream:
        writer=csv.writer(stream)
        writer.writerow(["elapsed_seconds","value_ms","source_x","source_y","kind"])
        for second,(y,kind,x) in sorted(snapped.items()):
            value=220+(Y220-y)*520/(Y220-Y740)
            writer.writerow([second,f"{value:.6f}",x,f"{y:.2f}",kind])
    calibration={"image_size":list(original.size),"plot_x":[X0,X1],"y_grid":{"220_ms":Y220,"740_ms":Y740},
                 "duration_seconds":DURATION,"observed_columns":len(upper),"knots":len(snapped),
                 "method":"Yellow-stroke mask, alternating local extrema of its upper/lower envelopes, 15-second timestamp snapping; linear interpolation between anchors.",
                 "unit":"ms","unit_confirmed_by_user":True,
                 "limitations":"Approximate screenshot reconstruction. One x pixel is about 19.2 seconds and one y pixel about 0.94 ms. Original sampling interval and exact timestamps are unknown. Overlapping subpixel tips cannot all be recovered; no random noise is added."}
    (ROOT / "calibration.json").write_text(json.dumps(calibration,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    overlay=original.copy()
    draw=ImageDraw.Draw(overlay)
    coords=[(X0+second/DURATION*(X1-X0),y) for second,(y,_,_) in sorted(snapped.items())]
    draw.line(coords,fill=(75,220,240),width=1)
    overlay.save(ROOT / "reconstruction-overlay.png")
    print(f"Extracted {len(snapped)} calibrated anchors from {len(upper)} raster columns")


if __name__ == "__main__":
    main()
