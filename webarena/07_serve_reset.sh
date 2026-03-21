#!/bin/bash
set -e

source 00_vars.sh

cd reset_server/
python3 server.py --port ${RESET_PORT} --init 2>&1 | tee -a server.log
