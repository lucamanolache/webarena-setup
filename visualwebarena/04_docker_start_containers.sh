#!/bin/bash

# stop if any error occur
set -e

source 00_vars.sh

wait_for_container() {
  local name="$1"
  local check="$2"
  local max=30
  local i=0
  until podman exec "$name" sh -c "$check" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge "$max" ]; then
      echo "ERROR: $name did not become ready"
      exit 1
    fi
    sleep 2
  done
  echo "  $name ready"
}

podman start shopping
podman start forum
podman start wikipedia
podman start classifieds_db
podman start classifieds

echo "Waiting for services to be ready..."
wait_for_container shopping "curl -sf http://localhost"
wait_for_container forum "curl -sf http://localhost"
wait_for_container wikipedia "curl -sf http://localhost"
wait_for_container classifieds "curl -sf http://localhost:9980"
echo "All services ready"
