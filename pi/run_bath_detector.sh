#!/bin/bash
cd "$(dirname "$0")"
source ../venv/bin/activate

export INFLUXDB_URL=http://phrpi.local:8086
export INFLUXDB_ORG=home
export INFLUXDB_BUCKET=span

# Load secrets (INFLUXDB_TOKEN, etc.) from .env
export $(grep -v '^#' .env | xargs)

python bath_detector.py "$@"
