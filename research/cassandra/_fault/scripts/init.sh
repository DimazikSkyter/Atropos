#!/usr/bin/env bash
set -Eeuo pipefail

expected="${EXPECTED_NODES:-3}"
echo "Waiting for $expected Cassandra nodes to converge..."

for attempt in $(seq 1 90); do
  if cqlsh cassandra1 9042 -e 'SELECT host_id FROM system.local;' >/dev/null 2>&1; then
    peer_count=$(cqlsh cassandra1 9042 -e 'SELECT peer FROM system.peers;' 2>/dev/null \
      | grep -Ec '^[[:space:]]*[0-9a-fA-F:.]+[[:space:]]*$' || true)
    visible=$((peer_count + 1))
    if (( visible >= expected )); then
      echo "Ring converged: $visible nodes visible."
      cqlsh cassandra1 9042 -f /lab/schema.cql
      echo "Schema is ready with RF=3."
      exit 0
    fi
  fi
  sleep 3
done

echo "Cluster did not converge to $expected nodes in time." >&2
exit 1

