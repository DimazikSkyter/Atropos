#!/usr/bin/env bash
set -Eeuo pipefail

until cqlsh cassandra1 9042 -e \
  "SELECT table_name FROM system_schema.tables WHERE keyspace_name='compaction_lab';" \
  | grep -q events; do
  echo "Waiting for compaction_lab.events..."
  sleep 3
done

echo "Starting workload: duration=${LOAD_DURATION:-30m}, threads=${LOAD_THREADS:-48}, writes=${WRITE_WEIGHT:-8}, reads=${READ_WEIGHT:-2}"
exec /opt/cassandra/tools/bin/cassandra-stress user \
  profile=/lab/stress.yaml \
  "duration=${LOAD_DURATION:-30m}" \
  no-warmup \
  "ops(insert=${WRITE_WEIGHT:-8},read_one=${READ_WEIGHT:-2})" \
  cl=ONE \
  -rate "threads=${LOAD_THREADS:-48}" \
  -node "${CONTACT_POINTS:-cassandra1,cassandra2,cassandra3}"
