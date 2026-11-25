"""Read the current replay and dashboard before creating the new response series."""
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.parse import urlencode

os.environ.pop('SSLKEYLOGFILE', None)
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
AUTH = {'Authorization': 'Basic ' + base64.b64encode(b'admin:admin').decode()}
TOKEN = os.getenv('INFLUXDB_TOKEN', 'atropos-local-replay-token')


def get(url, data=None, headers=None):
    with urlopen(Request(url, data=data, headers=headers or {}), timeout=60) as response:
        return response.read()


def flux(query):
    return get('http://127.0.0.1:8086/api/v2/query?' + urlencode({'org': 'atropos'}), query.encode(),
               {'Authorization': 'Token ' + TOKEN, 'Content-Type': 'application/vnd.flux', 'Accept': 'application/csv'})


def parse(raw):
    header, rows = None, []
    for fields in csv.reader(io.StringIO(raw.decode())):
        if not fields or fields[0].startswith('#'):
            continue
        if '_value' in fields:
            header = fields
        elif header:
            rows.append(dict(zip(header, fields)))
    return rows


def main():
    for uid, name in [('screenshot-steps-20261017', 'live-dashboard.json'), ('influxdb-replay', 'live-overview.json')]:
        (ROOT / 'pre-install-backup' / name).write_bytes(get('http://127.0.0.1:3001/api/dashboards/uid/' + uid, headers=AUTH))
    raw = flux('from(bucket: "metrics-replay") |> range(start: 0, stop: 2100-01-01T00:00:00Z) '
               '|> filter(fn: (r) => r._measurement == "screenshot_step_replay" or r._measurement == "screenshot_response_time_replay")')
    (ROOT / 'pre-install-backup/live-replay.csv').write_bytes(raw)
    rows = parse(raw)
    assert {r['_measurement'] for r in rows} == {'screenshot_step_replay'}, 'Response series already exists; inspect it before proceeding'
    assert len(rows) == 15824
    from datetime import datetime
    actual = {(r['trace'], int(datetime.fromisoformat(r['_time'].replace('Z', '+00:00')).timestamp())): float(r['_value']) for r in rows}
    with (ROOT / 'metrics.csv').open(encoding='utf-8') as handle:
        expected = {(r['trace'], int(datetime.fromisoformat(r['timestamp']).timestamp())): float(r['value']) for r in csv.DictReader(handle)}
    assert actual == expected, 'Disk TPS and live TPS differ'
    print(json.dumps({'live_tps_points': len(rows), 'response_series_exists': False, 'live_tps_sha256': hashlib.sha256(raw).hexdigest()}))


if __name__ == '__main__':
    main()
