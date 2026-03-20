#!/bin/bash

# stop if any error occur
set -e

source 00_vars.sh

WORKING_DIR=$(pwd)

podman create --name shopping -p $SHOPPING_PORT:80 shopping_final_0712
podman create --name shopping_admin -p $SHOPPING_ADMIN_PORT:80 shopping_admin_final_0719
podman create --name forum -p $REDDIT_PORT:80 postmill-populated-exposed-withimg
podman create --name gitlab -p $GITLAB_PORT:$GITLAB_PORT gitlab-populated-final-port8023 /opt/gitlab/embedded/bin/runsvdir-start --env GITLAB_PORT=$GITLAB_PORT
podman create --name wikipedia --volume=${WORKING_DIR}/wiki/:/data -p $WIKIPEDIA_PORT:80 ghcr.io/kiwix/kiwix-serve:3.3.0 wikipedia_en_all_maxi_2022-05.zim
