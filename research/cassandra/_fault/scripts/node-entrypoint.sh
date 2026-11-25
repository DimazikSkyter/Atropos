#!/usr/bin/env bash
set -Eeuo pipefail

cassandra_pid=""
reporter_pid=""
fs_exporter_pid=""

shutdown() {
  if [[ -n "$reporter_pid" ]]; then
    kill "$reporter_pid" 2>/dev/null || true
  fi
  if [[ -n "$fs_exporter_pid" ]]; then
    kill "$fs_exporter_pid" 2>/dev/null || true
  fi
  if [[ -n "$cassandra_pid" ]]; then
    kill -TERM "$cassandra_pid" 2>/dev/null || true
    wait "$cassandra_pid" 2>/dev/null || true
  fi
}
trap shutdown TERM INT

python3 /opt/lab/fs-exporter.py \
  --path /var/lib/cassandra \
  --port 9101 \
  --node "$(hostname)" &
fs_exporter_pid=$!

/usr/local/bin/docker-entrypoint.sh cassandra -f &
cassandra_pid=$!

until nodetool statusbinary 2>/dev/null | grep -q running; do
  if ! kill -0 "$cassandra_pid" 2>/dev/null; then
    wait "$cassandra_pid"
    exit $?
  fi
  sleep 2
done

nodetool setcompactionthroughput "${COMPACTION_MIB:-1}"
nodetool setconcurrentcompactors "${CONCURRENT_COMPACTORS:-1}"

report_interval="${LAB_REPORT_INTERVAL:-15}"
if [[ "$report_interval" != "0" ]]; then
  (
    while kill -0 "$cassandra_pid" 2>/dev/null; do
      echo "LAB_METRIC node=$(hostname) time=$(date -u +%FT%TZ)"
      nodetool compactionstats 2>&1 | sed -n '1,8p'
      df -h /var/lib/cassandra | tail -n 1
      sleep "$report_interval"
    done
  ) &
  reporter_pid=$!
fi

wait "$cassandra_pid"
exit_code=$?
if [[ -n "$reporter_pid" ]]; then
  kill "$reporter_pid" 2>/dev/null || true
fi
if [[ -n "$fs_exporter_pid" ]]; then
  kill "$fs_exporter_pid" 2>/dev/null || true
fi
exit "$exit_code"
