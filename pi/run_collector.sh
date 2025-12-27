#!/bin/bash
cd "$(dirname "$0")"
source ../venv/bin/activate

export INFLUXDB_URL=http://localhost:8086
export INFLUXDB_TOKEN=span-local-token
export INFLUXDB_ORG=home
export INFLUXDB_BUCKET=span
export SPAN_PANEL_IP=192.168.4.72
export POLL_INTERVAL=30

# Load SPAN token from .env
export $(grep -v '^#' .env | xargs)

python collector.py
