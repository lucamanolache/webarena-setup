#!/bin/bash
set -e
source 00_vars.sh

CONTAINERS=(shopping shopping_admin forum gitlab wikipedia)

for container in "${CONTAINERS[@]}"; do
  echo "Committing ${container}..."
  podman commit "$container" "${container}:${IMAGE_TAG}"
  echo "  done"
done

echo "All containers committed as images with tag :${IMAGE_TAG}"
