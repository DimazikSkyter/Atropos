import "date"

from(bucket: "metrics-replay")
  |> range(start: 2026-09-22T09:59:30Z, stop: date.add(d: 1ns, to: 2026-09-23T08:58:32Z))
  |> filter(fn: (r) => r._measurement == "rps" and r._field == "value")
  |> filter(fn: (r) => r.replay == "cusum-real-system-image")
  |> keep(columns: ["_time", "_value"])
  |> group(columns: [])
  |> sort(columns: ["_time"])