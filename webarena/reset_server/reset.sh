#!/bin/bash
# Thin wrapper for backwards compat / manual use.
# Passes any arguments as query string (e.g. reset.sh '?services=shopping,gitlab')
curl -s "http://localhost:7565/reset$1"
