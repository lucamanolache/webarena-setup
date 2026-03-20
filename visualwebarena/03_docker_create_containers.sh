#!/bin/bash

# stop if any error occur
set -e

source 00_vars.sh

WORKING_DIR=$(pwd)

podman create --name shopping -p $SHOPPING_PORT:80 shopping_final_0712
podman create --name forum -p $REDDIT_PORT:80 postmill-populated-exposed-withimg
podman create --name wikipedia --volume=${WORKING_DIR}/wiki/:/data -p $WIKIPEDIA_PORT:80 ghcr.io/kiwix/kiwix-serve:3.3.0 wikipedia_en_all_maxi_2022-05.zim

# Create podman network for classifieds (db <-> web communication)
podman network create classifieds-net 2>/dev/null || true

podman create --name classifieds_db \
  --network classifieds-net \
  -e MYSQL_ROOT_PASSWORD=password \
  -e MYSQL_DATABASE=osclass \
  -v ${WORKING_DIR}/classifieds_docker_compose/mysql:/docker-entrypoint-initdb.d \
  -v classifieds-db-data:/var/lib/mysql \
  mysql:8.1

podman create --name classifieds \
  --network classifieds-net \
  -p ${CLASSIFIEDS_PORT}:9980 \
  -e CLASSIFIEDS=${CLASSIFIEDS_URL}/ \
  -e RESET_TOKEN=4b61655535e7ed388f0d40a93600254c \
  jykoh/classifieds:latest
