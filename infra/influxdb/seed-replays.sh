#!/bin/sh
set -eu

# INIT_RETENTION applies only to a fresh volume. Update the existing bucket too.
listing=$(influx bucket list --org "$INFLUX_ORG" --name "$INFLUX_BUCKET_NAME" --hide-headers)
bucket_id=${listing%%[[:space:]]*}
case "$bucket_id" in
  ''|*[!0-9a-f]*) echo "Cannot find replay bucket: $INFLUX_BUCKET_NAME" >&2; exit 1 ;;
esac
[ "${#bucket_id}" -eq 16 ] || { echo "Unexpected bucket ID" >&2; exit 1; }
influx bucket update --id "$bucket_id" --retention "$INFLUX_RETENTION" --shard-group-duration 24h

# Replays use separate folders. Identical metric/tags/timestamps overwrite the
# same points, allowing imports to be rerun without duplicate history.
for replay_file in /replay/*.lp /replay/*/*.lp; do
  [ -f "$replay_file" ] || continue
  echo "Importing $replay_file"
  influx write --org "$INFLUX_ORG" --bucket "$INFLUX_BUCKET_NAME" --precision s --file "$replay_file"
done
