"""Read-only export of original InfluxDB values and account detector settings."""
import gzip
import hashlib
import json
import time
from pathlib import Path

from atropos_ui.influx import read_source
from atropos_ui.service_api import AccountStore

root = Path(__file__).resolve().parent
store = AccountStore('influx-test')
data = {}
skipped = 0
for source in store.sources():
    if source.kind == 'InfluxDB' and source.influx_version == '2':
        current, count = read_source(source)
        data.update(current)
        skipped += count
payload = {'fetched_at_unix': time.time(), 'metrics': data, 'account_metrics': store.metrics(),
           'skipped_nonnumeric_points': skipped}
encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
path = root / 'influx-input.json.gz'
path.write_bytes(gzip.compress(encoded))
metadata = {'measurements_fields': len(data), 'series': sum(len(m['series']) for m in data.values()),
            'points': sum(len(s['times']) for m in data.values() for s in m['series']),
            'decoded_sha256': hashlib.sha256(encoded).hexdigest(), 'gzip_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'read_only': True, 'fetched_at_unix': payload['fetched_at_unix']}
(root / 'influx-manifest.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(metadata, ensure_ascii=False, indent=2))
