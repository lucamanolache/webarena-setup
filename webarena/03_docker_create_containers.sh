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

# openstreetmap docker set up
cd openstreetmap-website/

# tile server URL (use default openstreetmap server)
OSM_TILE_SERVER_URL="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# geocoding server URL (use default openstreetmap server)
OSM_GEOCODING_SERVER_URL="https://nominatim.openstreetmap.org/"
# routing server URLs (use default openstreetmap server)
OSM_ROUTING_SERVER_URL="https://routing.openstreetmap.de"
OSM_CAR_SUFFIX="/routed-car"
OSM_BIKE_SUFFIX="/routed-bike"
OSM_FOOT_SUFFIX="/routed-foot"
# original WebArena config (CMU server with different ports for each vehicule type)
# OSM_ROUTING_SERVER_URL="http://metis.lti.cs.cmu.edu"
# OSM_CAR_SUFFIX=":5000"
# OSM_BIKE_SUFFIX=":5001"
# OSM_FOOT_SUFFIX=":5002"

# copy template files to be set up
cp ../openstreetmap-templates/docker-compose.yml ./docker-compose.yml
cp ../openstreetmap-templates/leaflet.osm.js ./vendor/assets/leaflet/leaflet.osm.js
cp ../openstreetmap-templates/fossgis_osrm.js ./app/assets/javascripts/index/directions/fossgis_osrm.js

# sed works differently on Mac (BSD) and Linux (GNU),
# so we need to check the version of sed to determine the correct syntax for in-place editing
if [[ -z "$OSTYPE" ]]; then
  echo "Error: OSTYPE is not set. Please run this script in a proper shell environment."
  exit 1
fi
if [[ "$OSTYPE" == "darwin"* ]]; then
  SED_INPLACE=(-i '')  # MacOS
else
  SED_INPLACE=(-i)  # Linux
fi
# set up web server port
sed "${SED_INPLACE[@]}" "s|MAP_PORT|${MAP_PORT}|g" docker-compose.yml
# set up tile server URL
sed "${SED_INPLACE[@]}" "s|url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'|url: '${OSM_TILE_SERVER_URL}'|g" ./vendor/assets/leaflet/leaflet.osm.js
# set up geocoding server URL
sed "${SED_INPLACE[@]}" "s|nominatim_url:.*|nominatim_url: \"$OSM_GEOCODING_SERVER_URL\"|g" ./config/settings.yml
# set up routing server URLs
sed "${SED_INPLACE[@]}" "s|fossgis_osrm_url:.*|fossgis_osrm_url: \"$OSM_ROUTING_SERVER_URL\"|g" ./config/settings.yml
sed "${SED_INPLACE[@]}" "s|__OSMCarSuffix__|${OSM_CAR_SUFFIX}|g" ./app/assets/javascripts/index/directions/fossgis_osrm.js
sed "${SED_INPLACE[@]}" "s|__OSMBikeSuffix__|${OSM_BIKE_SUFFIX}|g" ./app/assets/javascripts/index/directions/fossgis_osrm.js
sed "${SED_INPLACE[@]}" "s|__OSMFootSuffix__|${OSM_FOOT_SUFFIX}|g" ./app/assets/javascripts/index/directions/fossgis_osrm.js

# Create podman network for OSM (db <-> web communication)
podman network create osm-net 2>/dev/null || true

# OSM database
podman create --name openstreetmap-website-db-1 \
  --network osm-net \
  -p 54321:5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_DB=openstreetmap \
  -v osm-db-data:/var/lib/postgresql/data \
  openstreetmap-website-db

# OSM web server
podman create --name openstreetmap-website-web-1 \
  --network osm-net \
  -p ${MAP_PORT}:3000 \
  -e PIDFILE=/tmp/pids/server.pid \
  --tmpfs /tmp/pids/ \
  -v ${WORKING_DIR}/openstreetmap-website:/app \
  -v osm-web-tmp:/app/tmp \
  -v osm-web-storage:/app/storage \
  openstreetmap-website-web \
  bundle exec rails s -p 3000 -b '0.0.0.0'
