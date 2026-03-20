#!/bin/bash
set -e

cd ..
source 00_vars.sh

CHECKPOINT_DIR="${CHECKPOINT_DIR:-./checkpoints}"
CONTAINERS=(shopping shopping_admin forum gitlab wikipedia)

# Stop and remove existing containers
for container in "${CONTAINERS[@]}"; do
  podman stop "$container" 2>/dev/null || true
  podman rm "$container" 2>/dev/null || true
done

# Restore all containers from checkpoints in parallel
pids=()
for container in "${CONTAINERS[@]}"; do
  podman container restore \
    --import "${CHECKPOINT_DIR}/${container}.tar.gz" \
    --name "$container" \
    --tcp-established \
    --ignore-rootfs &
  pids+=($!)
done

# Wait for all restores
failed=0
for pid in "${pids[@]}"; do
  wait "$pid" || failed=1
done

if [ "$failed" -eq 1 ]; then
  echo "One or more container restores failed"
  exit 1
fi

echo "All containers restored from checkpoints"
