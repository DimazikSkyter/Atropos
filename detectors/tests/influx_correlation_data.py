"""Read the requested screenshot replays from Atropos InfluxDB, without CSV fallback."""
import csv
from datetime import datetime
import io
import json
import os
from urllib.parse import urlencode

os.environ.pop('SSLKEYLOGFILE', None)
from urllib.request import Request, urlopen

import numpy as np

MEASUREMENT = 'correlation_screenshot_replay'
CASES = {
    'latency': ('blue', 'green', 'yellow'),
    'read_operations': ('blue', 'orange', 'red', 'purple'),
}


def query(flux):
    host = os.getenv('INFLUX_URL', 'http://127.0.0.1:8086').rstrip('/')
    org = os.getenv('INFLUXDB_ORG', 'atropos')
    token = os.getenv('INFLUXDB_TOKEN', 'atropos-local-replay-token')
    request = Request(host + '/api/v2/query?' + urlencode({'org': org}),
        data=flux.encode(), headers={'Authorization': 'Token ' + token,
        'Content-Type': 'application/vnd.flux', 'Accept': 'application/csv'})
    with urlopen(request, timeout=60) as response:
        raw = response.read().decode()
    header, rows = None, []
    for fields in csv.reader(io.StringIO(raw)):
        if not fields or fields[0].startswith('#'):
            continue
        if '_time' in fields and '_value' in fields:
            header = fields
        elif header:
            rows.append(dict(zip(header, fields)))
    return rows


def load_case(case):
    """Return aligned Unix timestamps and a time-major finite matrix.

    All examples start on 18 Oct Moscow time. The overnight read graph ends
    on 19 Oct. Reject missing/duplicate/misaligned samples; never interpolate
    database output or silently substitute a fixture when InfluxDB is down.
    """
    traces = CASES[case]
    bucket = json.dumps(os.getenv('INFLUXDB_BUCKET', 'metrics-replay'))
    rows = query(f'''from(bucket: {bucket})
      |> range(start: 2026-10-17T21:00:00Z, stop: 2026-10-19T21:00:00Z)
      |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and
          r._field == "value" and r.scenario == "{case}")''')
    series = {name: {} for name in traces}
    for row in rows:
        name = row['trace']
        if name not in series:
            raise ValueError(f'unexpected trace {name}')
        stamp = int(datetime.fromisoformat(row['_time'].replace('Z', '+00:00')).timestamp())
        if stamp in series[name]:
            raise ValueError('duplicate InfluxDB timestamp')
        series[name][stamp] = float(row['_value'])
    stamps = sorted(series[traces[0]])
    if not stamps or any(sorted(s) != stamps for s in series.values()):
        raise ValueError('InfluxDB replay is empty or series are misaligned')
    stamps = np.asarray(stamps, dtype=np.int64)
    if len(set(np.diff(stamps))) != 1:
        raise ValueError('InfluxDB replay must have a uniform time grid')
    values = np.asarray([[series[name][t] for name in traces] for t in stamps])
    if not np.isfinite(values).all():
        raise ValueError('InfluxDB replay contains nonfinite values')
    return stamps, values
