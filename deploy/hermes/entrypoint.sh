#!/bin/sh
set -eu

python /opt/hermesgraph/bootstrap.py
exec "$@"
