#!/bin/bash
set -e
source 00_vars.sh

CHECKPOINT_DIR="${CHECKPOINT_DIR:-./checkpoints}"
mkdir -p "$CHECKPOINT_DIR"

CONTAINERS=(shopping shopping_admin forum gitlab wikipedia)

for container in "${CONTAINERS[@]}"; do
  echo "Checkpointing ${container}..."
  podman container checkpoint "$container" \
    --export "${CHECKPOINT_DIR}/${container}.tar.gz" \
    --tcp-established \
    --ignore-rootfs
  echo "  done"
done

echo "All checkpoints saved to ${CHECKPOINT_DIR}/"
