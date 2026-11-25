"""Digitize the seven visible TPS traces; numpy/Pillow are needed only for digitization."""
from pathlib import Path
import csv
import json
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
X_START, X_END = 112, 1642
Y_ZERO, Y_700 = 625.0, 62.0
TPS_PER_PIXEL = 700 / (Y_ZERO - Y_700)
rgb = np.asarray(Image.open(ROOT / 'reference.png').convert('RGB'), dtype=float)
r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
chroma = rgb.max(axis=2) - rgb.min(axis=2)
scores = {
    'green': (g-r) + .7*(g-b) + .5*g,
    'yellow': (r-b) + (g-b) - .4*np.abs(r-g) + .35*g,
    'gray': rgb.min(axis=2) - 1.7*chroma,
    'red': r - .5*(g+b) + .3*r,
    'olive': (r-b) + (g-b) - np.abs(r-g),
    'teal': g + b - 2*r,
    'orange': (r-g) + .8*(r-b) + .25*r,
}
green_ridge=g-.5*(np.roll(g,8,axis=0)+np.roll(g,-8,axis=0))
green_post_score=np.where((g-r>=3)&(g-b>=5), (g-r)+.7*(g-b)+.25*g+2*green_ridge, -10000)
# Each band is selected independently. The screenshot has no legend, so traces
# are named by color; no host/service identity is invented.
bands = {
    'green': ((40,125), (380,525), 700),
    'yellow': ((205,265), (515,590), 485),
    'gray': ((493,538), (523,585), 130),
    'red': ((549,565), (549,565), 82),
    'olive': ((564,586), (568,621), 65),
    'teal': ((588,602), (605,623), 30),
    'orange': ((601,618), (601,620), 18),
}

def select(score, x, low, high, previous=None):
    values = score[low:high+1, x].copy()
    if previous is not None:
        values -= .15*np.abs(np.arange(low, high+1)-previous)
    peak = values.max()
    chosen = np.flatnonzero(values >= peak - 1.0) + low
    return float(np.median(chosen)), float(peak)

# Visually calibrated center of the blurred vertical drop. The ±5 px source
# uncertainty is explicit; Cassandra event time is not used for digitization.
source_drop_x = 1516

traces, coverage = {}, {}
for name, (pre, post, nominal) in bands.items():
    ys, strengths = [], []
    previous = None
    for x in range(X_START,X_END+1):
        if x == source_drop_x:
            previous = None
        low, high = pre if x < source_drop_x else post
        score=green_post_score if name=='green' and x>=source_drop_x else scores[name]
        y, strength = select(score,x,low,high,None if name=='green' else previous)
        if name=='teal' and strength<=0:
            # The very faint stroke cannot be isolated by color in these
            # columns. Use its visually calibrated ~30/~10 TPS level, and
            # record that approximation rather than selecting a fill edge.
            y=Y_ZERO-(30 if x<source_drop_x else 10)/TPS_PER_PIXEL
        ys.append(y)
        strengths.append(strength)
        previous = y
    vals = (Y_ZERO-np.array(ys))*TPS_PER_PIXEL
    # The faint teal stroke is partially obscured by colored fills. Retain its
    # raster position; do not extrapolate a hidden rise from another trace.
    vals = np.maximum(vals,0)
    traces[name] = np.round(vals,3)
    coverage[name] = {'columns':len(vals),'median_color_score':round(float(np.median(strengths)),3),
                      'baseline_mean_tps':round(float(np.mean(vals[:source_drop_x-X_START])),3)}

with (ROOT / 'reference-traces.csv').open('w',encoding='utf-8',newline='') as handle:
    writer=csv.writer(handle)
    writer.writerow(['x',*bands])
    for i,x in enumerate(range(X_START,X_END+1)):
        writer.writerow([x,*[f'{traces[name][i]:.3f}' for name in bands]])
calibration={'source_width':int(rgb.shape[1]),'source_height':int(rgb.shape[0]),
             'x_start':X_START,'x_end':X_END,'source_drop_x':source_drop_x,
             'y_zero':Y_ZERO,'y_700':Y_700,'tps_per_pixel':TPS_PER_PIXEL,
             'traces':coverage,
             'source_drop_uncertainty_pixels':5,
             'limitations':'Approximate raster reconstruction. Legend is absent; identifiers are colors. '
                           'Faint/overlapping gray, olive, teal and orange lines are less precise than the dominant green/yellow traces. '
                           'Unresolvable teal columns use visually estimated 30 TPS before and 10 TPS after the partial drop.'}
(ROOT / 'calibration.json').write_text(json.dumps(calibration,indent=2)+'\n',encoding='utf-8')
print(json.dumps(calibration,indent=2))
