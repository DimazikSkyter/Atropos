import "date"

from(bucket: "metrics-replay")
  |> range(start: 2026-10-01T09:30:00Z, stop: date.add(d: 1ns, to: 2026-10-02T09:30:00Z))
  |> filter(fn: (r) => r._measurement == "cassandra_response_time" and r._field == "value")
  |> filter(fn: (r) => r.host =~ /^.*$/)
  |> keep(columns: ["_time", "_value", "host"])
  |> group(columns: ["host"])
  |> sort(columns: ["_time"])