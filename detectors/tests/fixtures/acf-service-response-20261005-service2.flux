import "date"

from(bucket: "metrics-replay")
  |> range(start: 2026-10-04T21:00:00Z, stop: date.add(d: 1ns, to: 2026-10-05T08:50:00Z))
  |> filter(fn: (r) => r._measurement == "service_response_time" and r._field == "value" and r.service == "service2")
  |> keep(columns: ["_time", "_value"])
  |> group(columns: [])
  |> sort(columns: ["_time"])