#!/bin/bash
set -e

source 00_vars.sh

cd reset_server/
# Extra args are forwarded to server.py, e.g.:
#   bash 07_serve_reset.sh --max-concurrent-boots 3
python3 server.py --port ${RESET_PORT} --init "$@" 2>&1 | tee -a server.log
