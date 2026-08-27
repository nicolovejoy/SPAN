#!/usr/bin/env python3
"""Detect bath events from heat pump power signature in InfluxDB."""

import argparse
import os
import time
import logging
from datetime import datetime, timezone, timedelta

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

import attribution
from hvac_classifier import query_timeline

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "home")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "span")

LOOKBACK_MINUTES = 90


def event_already_exists(query_api, event_start: datetime) -> bool:
    """Check if a bath_event already exists within +/-2 hours of the event start."""
    t_lo = (event_start - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_hi = (event_start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {t_lo}, stop: {t_hi})
  |> filter(fn: (r) => r._measurement == "bath_event")
  |> filter(fn: (r) => r._field == "duration_min")
  |> count()
'''
    tables = query_api.query(flux, org=INFLUXDB_ORG)
    for table in tables:
        for record in table.records:
            if record.get_value() > 0:
                return True
    return False


def write_bath_event(write_api, event: dict, status: str = "completed"):
    """Write a bath_event point to InfluxDB."""
    point = (
        Point("bath_event")
        .field("duration_min", event["duration_min"])
        .field("hp_mean_power_w", event["hp_mean_power_w"])
        .field("hp_max_power_w", event["hp_max_power_w"])
        .field("aux_active", event["aux_active"])
        .field("aux_mean_power_w", event["aux_mean_power_w"])
        .field("aux_max_power_w", event["aux_max_power_w"])
        .field("energy_kwh", event["energy_kwh"])
        .field("cost_dollars", event["cost_dollars"])
        .field("status", status)
        .time(event["start"])
    )
    write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)


def run_detection(query_api, start: str, stop: str = "now()") -> list[dict]:
    """Detect baths from the hvac_mode timeline (written by hvac_classifier)."""
    intervals = query_timeline(query_api, start, stop)
    logger.info(f"Queried {len(intervals)} timeline intervals")
    return attribution.bath_events(intervals)


def backtest(client: InfluxDBClient, days: int, write: bool = False):
    """Scan historical data day by day, print detections, optionally write to InfluxDB."""
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS) if write else None
    now = datetime.now(timezone.utc)
    total_events = 0
    written = 0

    for d in range(days, 0, -1):
        day_start = now - timedelta(days=d)
        day_end = now - timedelta(days=d - 1)
        start_str = day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        stop_str = day_end.strftime("%Y-%m-%dT%H:%M:%SZ")

        events = run_detection(query_api, start_str, stop_str)
        for ev in events:
            total_events += 1
            aux_str = "aux ON" if ev["aux_active"] else "aux off"
            print(
                f"  {ev['start'].strftime('%Y-%m-%d %H:%M')} - "
                f"{ev['end'].strftime('%H:%M')}  "
                f"{ev['duration_min']:.0f}min  "
                f"HP avg {ev['hp_mean_power_w']:.0f}W max {ev['hp_max_power_w']:.0f}W  "
                f"{aux_str}  {ev['energy_kwh']:.1f}kWh ${ev['cost_dollars']:.2f}"
            )
            if write_api:
                if event_already_exists(query_api, ev["start"]):
                    print("    ^ already exists, skipping")
                else:
                    write_bath_event(write_api, ev)
                    written += 1
                    print("    ^ written")

    print(f"\nTotal events detected: {total_events}")
    if write:
        print(f"Events written: {written}")


def normal_run(client: InfluxDBClient):
    """Check last 90 minutes, write new events to InfluxDB."""
    query_api = client.query_api()
    start_str = f"-{LOOKBACK_MINUTES}m"

    events = run_detection(query_api, start_str)
    if not events:
        logger.info("No bath events detected")
        return

    write_api = client.write_api(write_options=SYNCHRONOUS)
    for ev in events:
        if event_already_exists(query_api, ev["start"]):
            logger.info(f"Event at {ev['start']} already recorded, skipping")
            continue
        write_bath_event(write_api, ev)
        logger.info(
            f"Wrote bath_event: {ev['start'].strftime('%H:%M')}-{ev['end'].strftime('%H:%M')} "
            f"({ev['duration_min']:.0f}min, HP avg {ev['hp_mean_power_w']:.0f}W, "
            f"{ev['energy_kwh']:.1f}kWh, ${ev['cost_dollars']:.2f})"
        )


def main():
    parser = argparse.ArgumentParser(description="Detect bath events from heat pump data")
    parser.add_argument("--backtest", action="store_true", help="Scan historical data (no writes)")
    parser.add_argument("--backfill", action="store_true", help="Scan historical data and write events")
    parser.add_argument("--days", type=int, default=7, help="Days to scan in backtest/backfill mode")
    parser.add_argument("--loop", action="store_true", help="Run continuously with interval")
    parser.add_argument("--interval", type=int, default=600, help="Seconds between checks in loop mode")
    args = parser.parse_args()

    if not INFLUXDB_TOKEN:
        logger.error("INFLUXDB_TOKEN not set")
        return

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)

    if args.backtest or args.backfill:
        mode = "Backfill" if args.backfill else "Backtest"
        logger.info(f"{mode} mode: scanning last {args.days} days")
        backtest(client, args.days, write=args.backfill)
    elif args.loop:
        logger.info(f"Loop mode: checking every {args.interval}s")
        while True:
            try:
                normal_run(client)
            except Exception as e:
                logger.error(f"Detection error: {e}")
            time.sleep(args.interval)
    else:
        normal_run(client)

    client.close()


if __name__ == "__main__":
    main()
