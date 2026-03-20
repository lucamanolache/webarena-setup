#!/bin/bash

podman stop shopping_admin forum gitlab shopping wikipedia openstreetmap-website-db-1 openstreetmap-website-web-1
podman rm shopping_admin forum gitlab shopping wikipedia openstreetmap-website-db-1 openstreetmap-website-web-1
podman network rm osm-net 2>/dev/null || true
