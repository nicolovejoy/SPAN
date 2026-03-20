#!/usr/bin/env python3
"""Detect bath events from heat pump power signature in InfluxDB."""

import argparse
import os
import logging
from datetime import datetime, timezone, timedelta

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "home")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "span")

HP_CIRCUIT = "Heat pump (HP)"
AUX_CIRCUIT = "Auxiliary / Heat pump (HP)"

WINDOW_MINUTES = 15
STEP_MINUTES = 5
LOOKBACK_MINUTES = 90

# Detection thresholds
POWER_THRESHOLD = 50      # watts — on/off boundary
DUTY_CYCLE_MIN = 0.85     # fraction of samples above threshold
MAX_TRANSITIONS = 2       # on/off crossings in a window
MEAN_POWER_MIN = 500      # watts


def query_circuit_power(query_api, circuit_name: str, start: str, stop: str = "now()") -> list[dict]:
    """Query power_w samples for a circuit in the given time range."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "circuit")
  |> filter(fn: (r) => r.name == "{circuit_name}")
  |> filter(fn: (r) => r._field == "power_w")
  |> sort(columns: ["_time"])
'''
    tables = query_api.query(flux, org=INFLUXDB_ORG)
    results = []
    for table in tables:
        for record in table.records:
            results.append({"time": record.get_time(), "power": record.get_value()})
    return results


def analyze_window(samples: list[dict]) -> dict | None:
    """Compute duty cycle, transitions, and mean power for a list of samples."""
    if len(samples) < 3:
        return None

    powers = [abs(s["power"]) for s in samples]
    above = [p > POWER_THRESHOLD for p in powers]

    duty_cycle = sum(above) / len(above)
    transitions = sum(1 for i in range(1, len(above)) if above[i] != above[i - 1])
    mean_power = sum(powers) / len(powers)

    return {
        "duty_cycle": duty_cycle,
        "transitions": transitions,
        "mean_power": mean_power,
        "max_power": max(powers),
    }


def is_bath_like(stats: dict) -> bool:
    return (
        stats["duty_cycle"] >= DUTY_CYCLE_MIN
        and stats["transitions"] <= MAX_TRANSITIONS
        and stats["mean_power"] >= MEAN_POWER_MIN
    )


def find_bath_events(hp_samples: list[dict], aux_samples: list[dict]) -> list[dict]:
    """Scan overlapping windows and group consecutive bath-like windows into events."""
    if not hp_samples:
        return []

    t_start = hp_samples[0]["time"]
    t_end = hp_samples[-1]["time"]
    window = timedelta(minutes=WINDOW_MINUTES)
    step = timedelta(minutes=STEP_MINUTES)

    # Build windows
    windows = []
    w_start = t_start
    while w_start + window <= t_end:
        w_end = w_start + window
        w_samples = [s for s in hp_samples if w_start <= s["time"] < w_end]
        stats = analyze_window(w_samples)
        if stats:
            stats["window_start"] = w_start
            stats["window_end"] = w_end
            stats["bath_like"] = is_bath_like(stats)
            windows.append(stats)
        w_start += step

    # Group consecutive bath-like windows into events
    events = []
    current_run = []
    for w in windows:
        if w["bath_like"]:
            current_run.append(w)
        else:
            if len(current_run) >= 2:
                events.append(current_run)
            current_run = []
    if len(current_run) >= 2:
        events.append(current_run)

    # Build event records
    result = []
    for run in events:
        event_start = run[0]["window_start"]
        event_end = run[-1]["window_end"]
        duration_min = (event_end - event_start).total_seconds() / 60

        hp_mean = sum(w["mean_power"] for w in run) / len(run)
        hp_max = max(w["max_power"] for w in run)

        # Check aux heater activity in the same time range
        aux_in_range = [s for s in aux_samples if event_start <= s["time"] <= event_end]
        aux_powers = [abs(s["power"]) for s in aux_in_range]
        aux_active = any(p > POWER_THRESHOLD for p in aux_powers) if aux_powers else False
        aux_mean = sum(aux_powers) / len(aux_powers) if aux_powers else 0.0
        aux_max = max(aux_powers) if aux_powers else 0.0

        result.append({
            "start": event_start,
            "end": event_end,
            "duration_min": duration_min,
            "hp_mean_power_w": round(hp_mean, 1),
            "hp_max_power_w": round(hp_max, 1),
            "aux_active": aux_active,
            "aux_mean_power_w": round(aux_mean, 1),
            "aux_max_power_w": round(aux_max, 1),
        })

    return result


def event_already_exists(query_api, event_start: datetime) -> bool:
    """Check if a bath_event already exists within ±2 hours of the event start."""
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
        .field("status", status)
        .time(event["start"])
    )
    write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)


def run_detection(query_api, start: str, stop: str = "now()") -> list[dict]:
    """Query data and detect bath events in the given range."""
    hp_samples = query_circuit_power(query_api, HP_CIRCUIT, start, stop)
    aux_samples = query_circuit_power(query_api, AUX_CIRCUIT, start, stop)
    logger.info(f"Queried {len(hp_samples)} HP samples, {len(aux_samples)} aux samples")
    return find_bath_events(hp_samples, aux_samples)


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
                f"{aux_str}"
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
            f"({ev['duration_min']:.0f}min, HP avg {ev['hp_mean_power_w']:.0f}W)"
        )


def main():
    parser = argparse.ArgumentParser(description="Detect bath events from heat pump data")
    parser.add_argument("--backtest", action="store_true", help="Scan historical data (no writes)")
    parser.add_argument("--backfill", action="store_true", help="Scan historical data and write events")
    parser.add_argument("--days", type=int, default=7, help="Days to scan in backtest/backfill mode")
    args = parser.parse_args()

    if not INFLUXDB_TOKEN:
        logger.error("INFLUXDB_TOKEN not set")
        return

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)

    if args.backtest or args.backfill:
        mode = "Backfill" if args.backfill else "Backtest"
        logger.info(f"{mode} mode: scanning last {args.days} days")
        backtest(client, args.days, write=args.backfill)
    else:
        normal_run(client)

    client.close()


if __name__ == "__main__":
    main()
