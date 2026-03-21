#!/bin/bash

CONTAINERS=(shopping shopping_admin forum gitlab wikipedia openstreetmap-website-web-1 openstreetmap-website-db-1)

for container in "${CONTAINERS[@]}"; do
  podman stop "$container" 2>/dev/null || true
  podman rm "$container" 2>/dev/null || true
done

podman network rm osm-net 2>/dev/null || true
