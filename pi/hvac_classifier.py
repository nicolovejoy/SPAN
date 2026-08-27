#!/usr/bin/env python3
"""HVAC mode timeline service: classify 5-min intervals into the hvac_mode
measurement. Pure logic lives in hvac_modes.py; this file is Influx I/O + CLI.

Idempotency: hvac_mode points carry NO tags, so a rewrite at the same
timestamp overwrites in place (see weather_poller.write_weather_points for
why a tag would silently break this). The --loop pass re-classifies the
trailing 3h every pass, which self-heals missed passes and late data."""
import argparse
import os
import time
import logging
from datetime import datetime, timezone, timedelta

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

import attribution
import hvac_modes
from rates import cost_for_kwh

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "home")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "span")

HP_CIRCUIT = "Heat pump (HP)"
AUX_CIRCUIT = "Auxiliary / Heat pump (HP)"
TRAILING_WINDOW_HOURS = 3
MODES = ("heat", "cool", "hot_water", "idle", "ambiguous")

# hvac_modes.temp_at picks the nearest hourly reading; without padding, an
# interval at the very edge of the range has neighbours on one side only and
# can fall off the staleness cliff into "ambiguous" for no real reason.
WEATHER_PAD_HOURS = 2

# Same tolerance as bath_detector.event_already_exists -- the backtest is
# comparing against points that dedup was written under.
BATH_MATCH_TOLERANCE = timedelta(hours=2)

RFC3339 = "%Y-%m-%dT%H:%M:%SZ"


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(RFC3339)


def _now() -> datetime:
    """Thin wrapper so tests can freeze "now" via mock.patch.object."""
    return datetime.now(timezone.utc)


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


def query_weather(query_api, start: datetime, stop: datetime) -> list[dict]:
    """Hourly outdoor temperature over [start, stop], padded by
    +/-WEATHER_PAD_HOURS so hvac_modes.temp_at has a neighbour on both sides
    of every interval in the range. Written by weather_poller.py."""
    lo = _rfc3339(start - timedelta(hours=WEATHER_PAD_HOURS))
    hi = _rfc3339(stop + timedelta(hours=WEATHER_PAD_HOURS))
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {lo}, stop: {hi})
  |> filter(fn: (r) => r._measurement == "weather")
  |> filter(fn: (r) => r._field == "temp_f")
  |> sort(columns: ["_time"])
'''
    tables = query_api.query(flux, org=INFLUXDB_ORG)
    return [{"time": rec.get_time(), "temp_f": rec.get_value()}
            for table in tables for rec in table.records]


def classify_range(query_api, start: datetime, stop: datetime) -> list[dict]:
    """Raw HP + aux samples and hourly weather over [start, stop) -> classified
    5-min intervals. Pure bucketing/labelling is hvac_modes'."""
    start_str, stop_str = _rfc3339(start), _rfc3339(stop)
    hp = query_circuit_power(query_api, HP_CIRCUIT, start_str, stop_str)
    aux = query_circuit_power(query_api, AUX_CIRCUIT, start_str, stop_str)
    weather = query_weather(query_api, start, stop)
    intervals = hvac_modes.bucket_intervals(hp, aux, start, stop)
    return hvac_modes.classify(intervals, weather)


def write_intervals(write_api, intervals: list[dict]) -> int:
    """One untagged `hvac_mode` point per classified interval.

    NO TAGS, deliberately: in InfluxDB 2.x series identity is (measurement,
    tag set, field keys), so an untagged point written at an existing
    timestamp overwrites in place. That is what makes re-classifying an
    overlapping window (every --loop pass re-does the trailing 3h) safe with
    no existence check. Adding any tag -- mode= being the tempting one --
    would split a re-classified interval into a second series, leaving the
    stale label behind and double-counting its energy. `mode` is a FIELD.

    All five energy_<mode>_kwh fields are written every time, the interval's
    energy into its own mode's field and 0.0 into the other four, so field
    types and per-field coverage stay uniform across the measurement (a
    consumer summing energy_cool_kwh over a winter month gets 0.0, not a
    missing column)."""
    if not intervals:
        return 0
    for iv in intervals:
        mode = iv["mode"]
        energy = iv["energy_kwh"]
        point = Point("hvac_mode").field("mode", mode)
        for m in MODES:
            point = point.field(f"energy_{m}_kwh", energy if m == mode else 0.0)
        point = (point
                 .field("hp_mean_w", iv["hp_mean_w"])
                 .field("hp_max_w", iv["hp_max_w"])
                 .field("aux_mean_w", iv["aux_mean_w"])
                 .field("aux_max_w", iv["aux_max_w"])
                 .field("cost_dollars", cost_for_kwh(energy, iv["start"]))
                 .time(iv["start"]))
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
    return len(intervals)


def query_timeline(query_api, start: str, stop: str = "now()") -> list[dict]:
    """Read hvac_mode points back as classified-interval dicts, sorted by start.

    Pivoting on _field turns the one-row-per-field layout into one row per
    interval, which is what every consumer (attribution.runs, the web
    breakdown) actually wants. `energy_kwh` is the sum of the five mode
    fields -- exactly one is nonzero by construction, so the sum recovers the
    interval's energy without the caller having to know its mode."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "hvac_mode")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
    tables = query_api.query(flux, org=INFLUXDB_ORG)
    out = []
    for table in tables:
        for rec in table.records:
            v = rec.values
            out.append({
                "start": rec.get_time(),
                "mode": v.get("mode"),
                "hp_mean_w": v.get("hp_mean_w", 0.0),
                "hp_max_w": v.get("hp_max_w", 0.0),
                "aux_mean_w": v.get("aux_mean_w", 0.0),
                "aux_max_w": v.get("aux_max_w", 0.0),
                "energy_kwh": sum(v.get(f"energy_{m}_kwh") or 0.0 for m in MODES),
            })
    out.sort(key=lambda i: i["start"])
    return out


def query_bath_event_starts(query_api, start: datetime, stop: datetime) -> list[datetime]:
    """Timestamps of the bath_event points bath_detector already wrote over
    [start, stop) -- the ground truth the backtest compares against."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {_rfc3339(start)}, stop: {_rfc3339(stop)})
  |> filter(fn: (r) => r._measurement == "bath_event")
  |> filter(fn: (r) => r._field == "duration_min")
  |> sort(columns: ["_time"])
'''
    tables = query_api.query(flux, org=INFLUXDB_ORG)
    return [rec.get_time() for table in tables for rec in table.records]


def match_baths(detected: list[dict], historical_starts: list[datetime]) -> dict:
    """Greedy nearest-match of detected bath events against the historical
    bath_event starts, within BATH_MATCH_TOLERANCE.

    Each historical bath consumes at most one detection and vice versa:
    `missed` is a historical bath the new timeline did not reproduce,
    `extra` is a detection with no historical counterpart. Phase 0 wants to
    eyeball each diff, so both time lists come back too."""
    remaining = sorted(d["start"] for d in detected)
    used = [False] * len(remaining)
    matched = 0
    missed_times = []

    for h in sorted(historical_starts):
        best_i, best_gap = None, None
        for i, d in enumerate(remaining):
            if used[i]:
                continue
            gap = abs(d - h)
            if gap <= BATH_MATCH_TOLERANCE and (best_gap is None or gap < best_gap):
                best_i, best_gap = i, gap
        if best_i is None:
            missed_times.append(h)
        else:
            used[best_i] = True
            matched += 1

    extra_times = [d for i, d in enumerate(remaining) if not used[i]]
    return {
        "matched": matched,
        "missed": len(missed_times),
        "extra": len(extra_times),
        "missed_times": missed_times,
        "extra_times": extra_times,
    }


def _mode_counts(intervals: list[dict]) -> dict:
    return {m: sum(1 for i in intervals if i["mode"] == m) for m in MODES}


def _mode_energy(intervals: list[dict]) -> dict:
    return {m: sum(i["energy_kwh"] for i in intervals if i["mode"] == m) for m in MODES}


def _floor_5min(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0, minute=dt.minute - dt.minute % 5)


def normal_run(client: InfluxDBClient) -> None:
    """Re-classify the trailing TRAILING_WINDOW_HOURS and write. Re-doing an
    overlapping window every pass is the self-heal for a missed pass or for
    samples that landed late; untagged points make the overlap a no-op."""
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)
    stop = _floor_5min(_now())
    start = stop - timedelta(hours=TRAILING_WINDOW_HOURS)

    intervals = classify_range(query_api, start, stop)
    n = write_intervals(write_api, intervals)
    counts = _mode_counts(intervals)
    logger.info(f"Wrote {n} hvac_mode intervals ({start:%H:%M}-{stop:%H:%M} UTC): "
                + " ".join(f"{m} {counts[m]}" for m in MODES))


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _day_line(day_start: datetime, intervals: list[dict]) -> str:
    e = _mode_energy(intervals)
    return (f"{day_start:%Y-%m-%d}: heat {e['heat']:.1f} kWh "
            f"cool {e['cool']:.1f} hot_water {e['hot_water']:.1f} "
            f"idle {e['idle']:.1f} ambiguous {e['ambiguous']:.1f} "
            f"(n={len(intervals)})")


def backfill(client: InfluxDBClient, start_date: datetime) -> None:
    """Classify and write day by day, from start_date through yesterday (UTC).
    Whole days, so a re-run is a clean overwrite of the same intervals."""
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)
    yesterday, _ = _day_bounds(_now() - timedelta(days=1))
    day, _ = _day_bounds(start_date)

    total = 0
    while day <= yesterday:
        day_start, day_stop = _day_bounds(day)
        intervals = classify_range(query_api, day_start, day_stop)
        total += write_intervals(write_api, intervals)
        logger.info(_day_line(day_start, intervals))
        day += timedelta(days=1)
    logger.info(f"Backfill complete: {total} hvac_mode intervals written")


def backtest(client: InfluxDBClient, days: int, compare_baths: bool = False) -> None:
    """Classify the last `days` days and PRINT the result -- never write. This
    is the Phase 0 gate: the numbers get eyeballed before anything deploys."""
    query_api = client.query_api()
    # Complete UTC days ending yesterday -- the same boundary backfill writes
    # on, so a backtest number and the backfill it gates cover the same data.
    stop, _ = _day_bounds(_now())
    all_intervals = []

    for d in range(days, 0, -1):
        day_start = stop - timedelta(days=d)
        day_stop = day_start + timedelta(days=1)
        intervals = classify_range(query_api, day_start, day_stop)
        all_intervals.extend(intervals)
        print(_day_line(day_start, intervals))

    energy = _mode_energy(all_intervals)
    counts = _mode_counts(all_intervals)
    print(f"\nTotals over {days} days ({len(all_intervals)} intervals):")
    for m in MODES:
        print(f"  {m:<10} {counts[m]:>6} intervals  {energy[m]:>8.1f} kWh")

    if not compare_baths:
        return

    detected = attribution.bath_events(all_intervals)
    historical = query_bath_event_starts(query_api, stop - timedelta(days=days), stop)
    m = match_baths(detected, historical)
    print(f"\nBath comparison ({len(detected)} detected, {len(historical)} historical):")
    print(f"  matched {m['matched']}  missed {m['missed']}  extra {m['extra']}")
    for t in m["missed_times"]:
        print(f"  MISSED (historical, not detected): {t:%Y-%m-%d %H:%M} UTC")
    for t in m["extra_times"]:
        print(f"  EXTRA  (detected, no historical):  {t:%Y-%m-%d %H:%M} UTC")


def main():
    parser = argparse.ArgumentParser(description="HVAC mode timeline into InfluxDB")
    parser.add_argument("--backfill", action="store_true", help="Backfill historical intervals (no loop)")
    parser.add_argument("--start-date", type=str, default="2026-01-04",
                        help="Backfill start date YYYY-MM-DD (default: when circuit data starts)")
    parser.add_argument("--backtest", action="store_true", help="Classify and print, never write")
    parser.add_argument("--days", type=int, default=7, help="Days to scan in backtest mode")
    parser.add_argument("--compare-baths", action="store_true",
                        help="In backtest, compare detected baths against historical bath_event points")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=600, help="Seconds between passes in loop mode")
    args = parser.parse_args()

    if not INFLUXDB_TOKEN:
        logger.error("INFLUXDB_TOKEN not set")
        return

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)

    if args.backtest:
        logger.info(f"Backtest mode: last {args.days} days (no writes)")
        backtest(client, args.days, compare_baths=args.compare_baths)
    elif args.backfill:
        start = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        logger.info(f"Backfilling hvac_mode from {start:%Y-%m-%d}")
        backfill(client, start)
    elif args.loop:
        logger.info(f"Loop mode: classifying every {args.interval}s")
        while True:
            try:
                normal_run(client)
            except Exception as e:
                logger.error(f"Classification pass failed: {e}")
            time.sleep(args.interval)
    else:
        normal_run(client)

    client.close()


if __name__ == "__main__":
    main()
