from(bucket: "metrics-replay") |> range(start: 2026-10-17T11:23:30Z, stop: 2026-10-18T03:52:16Z)
 |> filter(fn: (r) => r._measurement == "screenshot_step_replay" and r._field == "value")
 |> keep(columns: ["_time", "_value", "trace"]) |> group(columns: ["trace"]) |> sort(columns: ["_time"])
