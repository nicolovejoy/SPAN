#!/usr/bin/env python3
"""SPAN panel data collector for InfluxDB."""

import os
import time
import logging
from datetime import datetime, timezone

import httpx
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from collector_health import classify_error

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


def fetch_panel_data(client: httpx.Client) -> dict:
    """Fetch panel-level data. Raises on failure."""
    try:
        headers = {"Authorization": f"Bearer {SPAN_TOKEN}"}
        response = client.get(f"{SPAN_BASE_URL}/panel", headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch panel data: {e}")
        raise


def fetch_circuits(client: httpx.Client) -> dict:
    """Fetch circuit data. Raises on failure."""
    try:
        headers = {"Authorization": f"Bearer {SPAN_TOKEN}"}
        response = client.get(f"{SPAN_BASE_URL}/circuits", headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch circuits: {e}")
        raise


def collect_and_write(http_client: httpx.Client, write_api) -> None:
    """Collect data from SPAN and write to InfluxDB."""
    now = datetime.now(timezone.utc)
    points = []

    # Fetch panel data
    t0 = time.monotonic()
    panel_err = None
    try:
        panel_data = fetch_panel_data(http_client)
        points.append(
            Point("panel")
            .field("grid_power_w", panel_data.get("instantGridPowerW", 0))
            .field("feedthrough_power_w", panel_data.get("feedthroughPowerW", 0))
            .field("consumed_energy_wh", panel_data.get("mainMeterEnergy", {}).get("consumedEnergyWh", 0))
            .field("produced_energy_wh", panel_data.get("mainMeterEnergy", {}).get("producedEnergyWh", 0))
            .time(now)
        )
    except Exception as e:
        panel_err = classify_error(e)
    panel_ms = int((time.monotonic() - t0) * 1000)

    # Fetch circuit data
    t0 = time.monotonic()
    circuits_err = None
    try:
        circuits_data = fetch_circuits(http_client)
        circuits = circuits_data.get("circuits", {})
        for circuit_id, circuit in circuits.items():
            name = circuit.get("name", "Unknown")
            points.append(
                Point("circuit")
                .tag("circuit_id", circuit_id)
                .tag("name", name)
                .field("power_w", circuit.get("instantPowerW", 0))
                .field("consumed_energy_wh", circuit.get("consumedEnergyWh", 0))
                .field("produced_energy_wh", circuit.get("producedEnergyWh", 0))
                .field("relay_state", 1 if circuit.get("relayState") == "CLOSED" else 0)
                .time(now)
            )
    except Exception as e:
        circuits_err = classify_error(e)
    circuits_ms = int((time.monotonic() - t0) * 1000)

    # Write to InfluxDB
    write_failed = False
    if points:
        try:
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=points)
            logger.info(f"Wrote {len(points)} points to InfluxDB")
        except Exception as e:
            logger.error(f"Failed to write to InfluxDB: {e}")
            write_failed = True

    # Emit a collector_poll point summarizing this iteration. Best-effort:
    # never let a failure here take down the poll loop.
    try:
        if write_failed:
            result = "write_fail"
        elif panel_err and circuits_err:
            result = "both_fail"
        elif panel_err:
            result = "panel_fail"
        elif circuits_err:
            result = "circuits_fail"
        else:
            result = "ok"
        error = circuits_err or panel_err or "none"

        poll_point = (
            Point("collector_poll")
            .tag("host", "phrpi")
            .tag("result", result)
            .tag("error", error)
            .field("panel_ms", panel_ms)
            .field("circuits_ms", circuits_ms)
            .field("points", len(points))
            .time(now)
        )
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=poll_point)
    except Exception as e:
        logger.error(f"Failed to write collector_poll point: {e}")


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
