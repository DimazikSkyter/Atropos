#!/bin/sh
set -eu

# Merge the existing providers with replay provisioning in a writable directory.
# Mounting new files inside an existing read-only bind fails on Docker Desktop.
mkdir -p "$GF_PATHS_PROVISIONING/datasources" "$GF_PATHS_PROVISIONING/dashboards"
cp -R /etc/grafana/provisioning-base/. "$GF_PATHS_PROVISIONING/"
cp /etc/grafana/replay/grafana-datasource.yml "$GF_PATHS_PROVISIONING/datasources/influxdb-replay.yml"
cp /etc/grafana/replay/grafana-dashboards.yml "$GF_PATHS_PROVISIONING/dashboards/influxdb-replay.yml"

exec /run.sh
