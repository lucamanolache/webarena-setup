#!/bin/bash

# stop if any error occur
set -e

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

podman start gitlab
podman start shopping
podman start shopping_admin
podman start forum
podman start wikipedia
podman start openstreetmap-website-db-1
podman start openstreetmap-website-web-1

echo "Waiting for services to be ready..."
wait_for_container shopping "curl -sf http://localhost"
wait_for_container shopping_admin "curl -sf http://localhost"
wait_for_container forum "curl -sf http://localhost"
wait_for_container wikipedia "curl -sf http://localhost"
wait_for_container openstreetmap-website-web-1 "curl -sf http://localhost:3000"
echo "All services ready"
