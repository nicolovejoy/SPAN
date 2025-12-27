#!/usr/bin/env python3
"""SPAN panel data collector for InfluxDB."""

import os
import time
import logging
from datetime import datetime, timezone

import httpx
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration from environment
SPAN_PANEL_IP = os.getenv("SPAN_PANEL_IP", "192.168.4.72")
SPAN_TOKEN = os.getenv("SPAN_ACCESS_TOKEN")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "home")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "span")

SPAN_BASE_URL = f"http://{SPAN_PANEL_IP}/api/v1"


def fetch_panel_data(client: httpx.Client) -> dict | None:
    """Fetch panel-level data."""
    try:
        headers = {"Authorization": f"Bearer {SPAN_TOKEN}"}
        response = client.get(f"{SPAN_BASE_URL}/panel", headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch panel data: {e}")
        return None


def fetch_circuits(client: httpx.Client) -> dict | None:
    """Fetch circuit data."""
    try:
        headers = {"Authorization": f"Bearer {SPAN_TOKEN}"}
        response = client.get(f"{SPAN_BASE_URL}/circuits", headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch circuits: {e}")
        return None


def collect_and_write(http_client: httpx.Client, write_api) -> None:
    """Collect data from SPAN and write to InfluxDB."""
    now = datetime.now(timezone.utc)
    points = []

    # Fetch panel data
    panel_data = fetch_panel_data(http_client)
    if panel_data:
        points.append(
            Point("panel")
            .field("grid_power_w", panel_data.get("instantGridPowerW", 0))
            .field("feedthrough_power_w", panel_data.get("feedthroughPowerW", 0))
            .field("consumed_energy_wh", panel_data.get("mainMeterEnergy", {}).get("consumedEnergyWh", 0))
            .field("produced_energy_wh", panel_data.get("mainMeterEnergy", {}).get("producedEnergyWh", 0))
            .time(now)
        )

    # Fetch circuit data
    circuits_data = fetch_circuits(http_client)
    if circuits_data:
        circuits = circuits_data.get("circuits", {})
        for circuit_id, circuit in circuits.items():
            points.append(
                Point("circuit")
                .tag("circuit_id", circuit_id)
                .tag("name", circuit.get("name", "Unknown"))
                .field("power_w", circuit.get("instantPowerW", 0))
                .field("consumed_energy_wh", circuit.get("consumedEnergyWh", 0))
                .field("produced_energy_wh", circuit.get("producedEnergyWh", 0))
                .field("relay_state", 1 if circuit.get("relayState") == "CLOSED" else 0)
                .time(now)
            )

    # Write to InfluxDB
    if points:
        try:
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=points)
            logger.info(f"Wrote {len(points)} points to InfluxDB")
        except Exception as e:
            logger.error(f"Failed to write to InfluxDB: {e}")


def main():
    if not SPAN_TOKEN:
        logger.error("SPAN_ACCESS_TOKEN not set")
        return

    if not INFLUXDB_TOKEN:
        logger.error("INFLUXDB_TOKEN not set")
        return

    logger.info(f"Starting collector: SPAN={SPAN_PANEL_IP}, InfluxDB={INFLUXDB_URL}")
    logger.info(f"Poll interval: {POLL_INTERVAL}s")

    influx_client = InfluxDBClient(
        url=INFLUXDB_URL,
        token=INFLUXDB_TOKEN,
        org=INFLUXDB_ORG
    )
    write_api = influx_client.write_api(write_options=SYNCHRONOUS)

    with httpx.Client(timeout=10.0) as http_client:
        while True:
            try:
                collect_and_write(http_client, write_api)
            except Exception as e:
                logger.error(f"Collection error: {e}")

            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
