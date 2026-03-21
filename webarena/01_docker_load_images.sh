#!/bin/bash

# stop if any error occur
set -e

source 00_vars.sh

assert() {
  if ! "$@"; then
    echo "Assertion failed: $@" >&2
    exit 1
  fi
}

load_docker_image() {
  local IMAGE_NAME="$1"
  local INPUT_FILE="$2"

  if ! podman images --format "{{.Repository}}:{{.Tag}}" | grep -q "^${IMAGE_NAME}:"; then
    echo "Loading image ${IMAGE_NAME} from ${INPUT_FILE}"
    podman load --input "${INPUT_FILE}"
  else
    echo "Image ${IMAGE_NAME} is already loaded."
  fi
}

# make sure all required files are here
assert [ -f ${ARCHIVES_LOCATION}/shopping_final_0712.tar ]
assert [ -f ${ARCHIVES_LOCATION}/shopping_admin_final_0719.tar ]
assert [ -f ${ARCHIVES_LOCATION}/postmill-populated-exposed-withimg.tar ]
assert [ -f ${ARCHIVES_LOCATION}/gitlab-populated-final-port8023.tar ]
assert [ -f ${ARCHIVES_LOCATION}/openstreetmap-website-db.tar.gz ]
assert [ -f ${ARCHIVES_LOCATION}/openstreetmap-website-web.tar.gz ]
assert [ -f ${ARCHIVES_LOCATION}/openstreetmap-website.tar.gz ]
assert [ -f ${ARCHIVES_LOCATION}/wikipedia_en_all_maxi_2022-05.zim ]

# load docker images in parallel
load_docker_image "shopping_final_0712" "${ARCHIVES_LOCATION}/shopping_final_0712.tar" &
load_docker_image "shopping_admin_final_0719" "${ARCHIVES_LOCATION}/shopping_admin_final_0719.tar" &
load_docker_image "postmill-populated-exposed-withimg" "${ARCHIVES_LOCATION}/postmill-populated-exposed-withimg.tar" &
load_docker_image "gitlab-populated-final-port8023" "${ARCHIVES_LOCATION}/gitlab-populated-final-port8023.tar" &
load_docker_image "openstreetmap-website-db" "${ARCHIVES_LOCATION}/openstreetmap-website-db.tar.gz" &
load_docker_image "openstreetmap-website-web" "${ARCHIVES_LOCATION}/openstreetmap-website-web.tar.gz" &

# pull kiwix-serve image for wikipedia (not loaded from tar)
if ! podman images --format "{{.Repository}}:{{.Tag}}" | grep -q "^ghcr.io/kiwix/kiwix-serve:3.3.0"; then
  echo "Pulling kiwix-serve image..."
  podman pull ghcr.io/kiwix/kiwix-serve:3.3.0 &
else
  echo "kiwix-serve image already present."
fi

# extract openstreetmap archive locally (if needed)
if [ ! -d ./openstreetmap-website ]; then
  echo "Extracting openstreetmap archive..."
  tar -xzf ${ARCHIVES_LOCATION}/openstreetmap-website.tar.gz &
else
  echo "Openstreetmap archive already extracted."
fi

# copy wikipedia archive to local folder (if needed)
# use cp -L to dereference symlinks (container bind mounts can't follow symlinks outside the mount)
WIKIPEDIA_ARCHIVE=wikipedia_en_all_maxi_2022-05.zim
if [ ! -f ./wiki/${WIKIPEDIA_ARCHIVE} ]; then
  echo "Copying wikipedia archive..."
  mkdir -p ./wiki
  cp -L "${ARCHIVES_LOCATION}/${WIKIPEDIA_ARCHIVE}" ./wiki/ &
else
  echo "Wikipedia archive already present."
fi

# wait for all background jobs
wait
echo "All images loaded."
