#!/bin/bash

# stop if any error occur
set -e

source 00_vars.sh

wait_for_container() {
  local name="$1"
  local check="$2"
  local max="${3:-30}"
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

wait_for_host_url() {
  local name="$1"
  local url="$2"
  local max="${3:-30}"
  local i=0
  until curl -sf "$url" >/dev/null 2>&1; do
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
# kiwix-serve container has no curl, check from host
wait_for_host_url wikipedia "http://localhost:${WIKIPEDIA_PORT}"
wait_for_container openstreetmap-website-web-1 "curl -sf http://localhost:3000"
# gitlab takes longer to boot
wait_for_host_url gitlab "http://localhost:${GITLAB_PORT}" 120
echo "All services ready"
