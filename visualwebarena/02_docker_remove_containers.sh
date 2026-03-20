#!/bin/bash

podman stop classifieds_db classifieds forum shopping wikipedia
podman rm classifieds_db classifieds forum shopping wikipedia
podman network rm classifieds-net 2>/dev/null || true
