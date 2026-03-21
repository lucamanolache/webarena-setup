#!/bin/bash
set -e

cd ..
source 00_vars.sh

WORKING_DIR=$(pwd)
CONTAINERS=(shopping shopping_admin forum gitlab wikipedia)

# Stop and remove existing containers
for container in "${CONTAINERS[@]}"; do
  podman stop "$container" 2>/dev/null || true
  podman rm "$container" 2>/dev/null || true
done

# Recreate from committed images
podman create --name shopping -p $SHOPPING_PORT:80 "shopping:${IMAGE_TAG}"
podman create --name shopping_admin -p $SHOPPING_ADMIN_PORT:80 "shopping_admin:${IMAGE_TAG}"
podman create --name forum -p $REDDIT_PORT:80 "forum:${IMAGE_TAG}"
podman create --name gitlab -p $GITLAB_PORT:$GITLAB_PORT "gitlab:${IMAGE_TAG}" /opt/gitlab/embedded/bin/runsvdir-start --env GITLAB_PORT=$GITLAB_PORT
podman create --name wikipedia --volume=${WORKING_DIR}/wiki/:/data -p $WIKIPEDIA_PORT:80 "wikipedia:${IMAGE_TAG}" wikipedia_en_all_maxi_2022-05.zim

# Start all
for container in "${CONTAINERS[@]}"; do
  podman start "$container"
done

echo "All containers reset from committed images"
