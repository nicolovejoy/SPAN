#!/usr/bin/env python3
"""Detect EV charging sessions from Tesla charger circuit power data."""

import argparse
import os
import time
import logging
from datetime import datetime, timezone, timedelta

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from rates import cost_for_kwh

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "home")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "span")

CIRCUIT_NAME = os.getenv("CHARGE_CIRCUIT", "Outdoor / Tesla Car Charger")
POWER_THRESHOLD = 1000  # watts
MIN_DURATION_MIN = 30
LOOKBACK_MINUTES = 180


def query_circuit_power(query_api, start: str, stop: str = "now()") -> list[dict]:
    """Query power samples for the charger circuit."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "circuit")
  |> filter(fn: (r) => r.name == "{CIRCUIT_NAME}")
  |> filter(fn: (r) => r._field == "power_w")
  |> sort(columns: ["_time"])
'''
    results = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            results.append({"time": record.get_time(), "power": abs(record.get_value())})
    return results


def find_charge_sessions(samples: list[dict]) -> list[dict]:
    """Find contiguous periods above threshold lasting >= MIN_DURATION_MIN."""
    if not samples:
        return []

    sessions = []
    run_start = None
    run_samples = []

    for s in samples:
        if s["power"] >= POWER_THRESHOLD:
            if run_start is None:
                run_start = s["time"]
            run_samples.append(s)
        else:
            if run_start and run_samples:
                _maybe_add_session(sessions, run_start, run_samples)
            run_start = None
            run_samples = []

    if run_start and run_samples:
        _maybe_add_session(sessions, run_start, run_samples)

    return sessions


def _maybe_add_session(sessions: list, run_start, run_samples: list):
    duration = (run_samples[-1]["time"] - run_start).total_seconds() / 60
    if duration < MIN_DURATION_MIN:
        return
    powers = [x["power"] for x in run_samples]
    mean_power = sum(powers) / len(powers)
    energy_kwh = mean_power * duration / 60 / 1000
    sessions.append({
        "start": run_start,
        "end": run_samples[-1]["time"],
        "duration_min": round(duration, 1),
        "mean_power_w": round(mean_power, 1),
        "max_power_w": round(max(powers), 1),
        "energy_kwh": round(energy_kwh, 3),
        "cost_dollars": round(cost_for_kwh(energy_kwh, run_start), 2),
    })


def event_already_exists(query_api, event_start: datetime) -> bool:
    """Check if a charge_event already exists within +/-2 hours."""
    t_lo = (event_start - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_hi = (event_start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {t_lo}, stop: {t_hi})
  |> filter(fn: (r) => r._measurement == "charge_event")
  |> filter(fn: (r) => r._field == "duration_min")
  |> count()
'''
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            if record.get_value() > 0:
                return True
    return False


def write_charge_event(write_api, event: dict):
    """Write a charge_event point to InfluxDB."""
    point = (
        Point("charge_event")
        .field("duration_min", event["duration_min"])
        .field("mean_power_w", event["mean_power_w"])
        .field("max_power_w", event["max_power_w"])
        .field("energy_kwh", event["energy_kwh"])
        .field("cost_dollars", event["cost_dollars"])
        .time(event["start"])
    )
    write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)


def normal_run(client: InfluxDBClient):
    """Check recent data, write new charge events."""
    query_api = client.query_api()
    samples = query_circuit_power(query_api, f"-{LOOKBACK_MINUTES}m")
    sessions = find_charge_sessions(samples)

    if not sessions:
        logger.info("No charging sessions detected")
        return

    write_api = client.write_api(write_options=SYNCHRONOUS)
    for s in sessions:
        if event_already_exists(query_api, s["start"]):
            logger.info(f"Session at {s['start']} already recorded, skipping")
            continue
        write_charge_event(write_api, s)
        logger.info(
            f"Wrote charge_event: {s['start'].strftime('%H:%M')}-{s['end'].strftime('%H:%M')} "
            f"({s['duration_min']:.0f}min, {s['mean_power_w']:.0f}W, "
            f"{s['energy_kwh']:.1f}kWh, ${s['cost_dollars']:.2f})"
        )


def backtest(client: InfluxDBClient, days: int, write: bool = False):
    """Scan historical data day by day."""
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS) if write else None
    now = datetime.now(timezone.utc)
    total = written = 0

    for d in range(days, 0, -1):
        day_start = now - timedelta(days=d)
        day_end = now - timedelta(days=d - 1)
        start_str = day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        stop_str = day_end.strftime("%Y-%m-%dT%H:%M:%SZ")

        samples = query_circuit_power(query_api, start_str, stop_str)
        sessions = find_charge_sessions(samples)
        for s in sessions:
            total += 1
            print(
                f"  {s['start'].strftime('%Y-%m-%d %H:%M')} - {s['end'].strftime('%H:%M')}  "
                f"{s['duration_min']:.0f}min  {s['mean_power_w']:.0f}W  "
                f"{s['energy_kwh']:.1f}kWh  ${s['cost_dollars']:.2f}"
            )
            if write_api:
                if event_already_exists(query_api, s["start"]):
                    print("    ^ already exists, skipping")
                else:
                    write_charge_event(write_api, s)
                    written += 1
                    print("    ^ written")

    print(f"\nTotal sessions detected: {total}")
    if write:
        print(f"Sessions written: {written}")


def main():
    parser = argparse.ArgumentParser(description="Detect EV charging sessions")
    parser.add_argument("--backtest", action="store_true", help="Scan historical data (no writes)")
    parser.add_argument("--backfill", action="store_true", help="Scan historical data and write events")
    parser.add_argument("--days", type=int, default=7, help="Days to scan")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=600, help="Seconds between checks")
    args = parser.parse_args()

    if not INFLUXDB_TOKEN:
        logger.error("INFLUXDB_TOKEN not set")
        return

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)

    if args.backtest or args.backfill:
        mode = "Backfill" if args.backfill else "Backtest"
        logger.info(f"{mode}: scanning last {args.days} days")
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
