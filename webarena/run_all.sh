#!/bin/bash
# Runs the full webarena stack: initial setup (if needed) + reset server + homepage.
# Usage: sudo bash run_all.sh [--setup]
#   --setup: Run the full initial setup (load images, create containers, patch, checkpoint)
#            Only needed on first run or to rebuild the :ready images.
#   Without --setup: Just starts the reset server and homepage (assumes :ready images exist).
set -e

cd "$(dirname "$0")"
source 00_vars.sh

if [ "$1" = "--setup" ]; then
    echo "=== Running full initial setup ==="
    bash 01_docker_load_images.sh
    bash 02_docker_remove_containers.sh
    bash 03_docker_create_containers.sh
    bash 04_docker_start_containers.sh
    bash 05_docker_patch_containers.sh
    bash 08_checkpoint.sh
    # Stop all containers — the reset server will manage them from here
    podman stop -a 2>/dev/null || true
    echo "=== Initial setup complete ==="
fi

# Start homepage in background
echo "Starting homepage server on port ${HOMEPAGE_PORT}..."
bash 06_serve_homepage.sh &
HOMEPAGE_PID=$!

# Start reset server (blocking — manages all containers)
echo "Starting reset server on port ${RESET_PORT}..."
bash 07_serve_reset.sh &
RESET_PID=$!

# Wait for either to exit
trap "kill $HOMEPAGE_PID $RESET_PID 2>/dev/null; wait" EXIT INT TERM
wait
