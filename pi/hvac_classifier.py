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

# Nightly self-heal sweep (#14 sub-project 2 addendum): --loop's trailing 3h
# window can't reach an outage longer than 3h, which leaves a permanent hole
# only a manual --backfill repairs -- the same dead-service blind spot
# CLAUDE.md already records against weather_poller.py. Re-run the last two
# completed LOCAL days through the existing --backfill path once nightly.
#
# 02:00 local, not right after midnight: a DHW run straddling the day
# boundary needs to be over before its length is measurable (that's what
# distinguishes a reheat from space conditioning), and DHW_RUN_MAX_MINUTES is
# 120 -- a 2h lag covers the worst case with margin. "Local" here means the
# container's TZ (America/Los_Angeles, set in docker-compose.yml), the same
# mechanism daily_report.py's seconds_until_hour relies on -- datetime.now()
# without a tz reads the OS/libc local clock, which follows the TZ env var
# once tzdata is installed (it is, in the Dockerfile).
#
# Getting FULL coverage of the last two completed Pacific days needs two
# pieces, not just a start-date offset:
#   - LOWER bound: `run_sweep` builds `start` from sweep_date's y/m/d
#     relabelled as a UTC date, minus SWEEP_LOOKBACK_DAYS. Pacific day D-2
#     always begins a few hours AFTER UTC midnight on the same date digits
#     (00:00 Pacific D-2 = 07:00 UTC D-2 in PDT, 08:00 UTC D-2 in PST), so
#     starting the UTC-day loop at date D-2 already covers it -- 2 is
#     correct here and does not need to become 3.
#   - UPPER bound: `backfill()` on its own always stops at UTC-yesterday
#     relative to whenever it runs, i.e. it never touches the day's own UTC
#     calendar date. At 02:00 Pacific the current UTC date is the SAME as
#     the Pacific one (2am Pacific + 7/8h offset is still well before UTC
#     midnight rolls the date again), so "UTC yesterday" ends at UTC
#     midnight -- which is only 16:00/17:00 Pacific on Pacific day D-1, i.e.
#     the bath-hour evening of the day that JUST completed is still short by
#     up to 8h. Bumping SWEEP_LOOKBACK_DAYS cannot fix this: it only pushes
#     the START further into the past, it can't move an END that isn't
#     derived from it at all. `run_sweep` therefore passes `end_date=_now()`
#     into `backfill()`, which extends the day-loop through today's (still
#     in progress) UTC date, capped at `now` so it never writes placeholder
#     data for hours that have not happened yet.
#
# Net effect verified both ways:
#   PST (UTC-8): 02:00 Pacific D = 10:00 UTC D. Need UTC[D-2+8h, D+8h).
#     Coverage = UTC[D-2 00:00 (start), D 10:00 (now, capped)) -- superset.
#   PDT (UTC-7): 02:00 Pacific D = 09:00 UTC D. Need UTC[D-2+7h, D+7h).
#     Coverage = UTC[D-2 00:00 (start), D 09:00 (now, capped)) -- superset.
SWEEP_HOUR = 2
SWEEP_LOOKBACK_DAYS = 2

# Classification is NOT interval-local: hvac_modes._mark_hot_water measures the
# length of a contiguous DHW-shaped run, so an interval sitting at a window's
# leading edge has its run truncated to whatever the window happens to contain.
# A truncated run shorter than DHW_RUN_MIN_MINUTES fails the DHW test and falls
# through to temperature, so the TAIL of every hot-water run would be relabelled
# heat/cool by the one pass that sees it with the least context -- and since
# that is also the LAST pass to touch it, the wrong label sticks forever.
#
# Fix on both code paths: classify a padded range, write only the unpadded core.
#   - rolling pass: classify [T-3h, T], write only start >= T-3h+WRITE_LEAD_IN.
#     Interval X is then written by passes T in (X, X+2h]; its final write (at
#     T = X+2h) sees the window [X-1h, X+2h] -- 1h of lead-in, 2h of lead-out.
#   - day batch: classify [day-1h, day+1d+1h], write only intervals in the day.
#     Both sides matter: a run straddling midnight UTC (= 16:00/17:00 Pacific, a
#     plausible bath hour) needs the NEXT day's intervals to measure its true
#     length, not just the previous day's.
# Neither buffer is free of edge cases -- a run longer than DHW_RUN_MAX_MINUTES
# truncated down into the accepted band can still slip through -- but it moves
# the context available at the deciding write from zero to an hour.
WRITE_LEAD_IN_HOURS = 1
BATCH_PAD_HOURS = 1

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


def _local_now() -> datetime:
    """Thin wrapper so tests can freeze local "now" via mock.patch.object.
    Naive on purpose -- see the SWEEP_HOUR comment above."""
    return datetime.now()


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
    missing column).

    Points go out as one batched write per call, not one HTTP round-trip per
    point -- callers batch a whole day at a time (288 intervals), and the
    230-day backfill would otherwise be ~66k synchronous round-trips."""
    if not intervals:
        return 0
    points = []
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
        points.append(point)
    write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=points)
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


def _trim(intervals: list[dict], lo: datetime, hi: datetime | None = None) -> list[dict]:
    """Keep only the intervals inside [lo, hi) -- the unpadded core of a range
    that was deliberately classified wider than it is written. See the
    WRITE_LEAD_IN_HOURS / BATCH_PAD_HOURS note at the top of the module."""
    return [i for i in intervals
            if i["start"] >= lo and (hi is None or i["start"] < hi)]


def normal_run(client: InfluxDBClient) -> None:
    """Re-classify the trailing TRAILING_WINDOW_HOURS and write. Re-doing an
    overlapping window every pass is the self-heal for a missed pass or for
    samples that landed late; untagged points make the overlap a no-op.

    Only the intervals past WRITE_LEAD_IN_HOURS into the window are written:
    the first hour is lead-in context for hvac_modes' run-length logic, not
    output. Skipping it here costs nothing -- those intervals were already
    written (better informed) by earlier passes."""
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)
    stop = _floor_5min(_now())
    start = stop - timedelta(hours=TRAILING_WINDOW_HOURS)

    intervals = classify_range(query_api, start, stop)
    writable = _trim(intervals, start + timedelta(hours=WRITE_LEAD_IN_HOURS))
    n = write_intervals(write_api, writable)
    counts = _mode_counts(writable)
    logger.info(f"Wrote {n} hvac_mode intervals "
                f"({(start + timedelta(hours=WRITE_LEAD_IN_HOURS)):%H:%M}-{stop:%H:%M} UTC, "
                f"{len(intervals) - n} lead-in intervals skipped): "
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


def classify_day(query_api, day_start: datetime) -> list[dict]:
    """One UTC day's classified intervals, classified with BATCH_PAD_HOURS of
    context on BOTH sides and then trimmed back to the day itself. The padding
    is what keeps a DHW run straddling midnight UTC from being split into two
    fragments too short to pass DHW_RUN_MIN_MINUTES.

    backfill and backtest both go through here, so the Phase 0 gate reports
    exactly the intervals the backfill it gates would write."""
    pad = timedelta(hours=BATCH_PAD_HOURS)
    day_stop = day_start + timedelta(days=1)
    intervals = classify_range(query_api, day_start - pad, day_stop + pad)
    return _trim(intervals, day_start, day_stop)


def backfill(client: InfluxDBClient, start_date: datetime, end_date: datetime | None = None) -> None:
    """Classify and write day by day, from start_date through end_date (UTC),
    defaulting to yesterday when end_date is omitted -- the historical
    230-day backfill and the --backfill CLI both rely on that default to
    never touch the still-accumulating current day. Whole days, so a re-run
    is a clean overwrite of the same intervals -- EXCEPT the day matching
    today's UTC date when an explicit end_date reaches it (only run_sweep
    does this): that day is capped at `now`, not written out to its full
    24h, so a partial day never gets placeholder zero-power "idle" intervals
    for hours that have not happened yet. The next call (loop pass, tomorrow
    night's sweep) fills the rest in once it has actually occurred.

    One day's classify/write failing (a transient Influx error, say, at day
    180 of 230) is logged and skipped rather than aborting the whole run --
    the operator would otherwise have no record of where to resume beyond
    the last `_day_line` and would have to infer it. `failed` in the summary
    line makes a clean run vs. a partial one visible at a glance."""
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)
    now = _now()
    today, _ = _day_bounds(now)
    last_day, _ = _day_bounds(end_date) if end_date is not None else _day_bounds(now - timedelta(days=1))
    day, _ = _day_bounds(start_date)

    total = 0
    failed = 0
    while day <= last_day:
        day_start, _ = _day_bounds(day)
        try:
            intervals = classify_day(query_api, day_start)
            if day_start == today:
                intervals = [iv for iv in intervals if iv["start"] < now]
            total += write_intervals(write_api, intervals)
            logger.info(_day_line(day_start, intervals))
        except Exception as e:
            failed += 1
            logger.error(f"Backfill failed for {day_start:%Y-%m-%d}, skipping: {e}")
        day += timedelta(days=1)
    summary = f"Backfill complete: {total} hvac_mode intervals written"
    if failed:
        summary += f", {failed} day(s) failed (see errors above for dates)"
    logger.info(summary)


def sweep_due(now_local: datetime, last_sweep_date) -> "datetime.date | None":
    """Pure scheduling decision for the nightly self-heal sweep: the local
    calendar date to sweep for, or None if it isn't time yet.

    Fires at most once per local calendar day, only once the clock has passed
    SWEEP_HOUR local -- see the module-level comment for why. `last_sweep_date`
    is the date (or None) the caller last actually ran a sweep for; comparing
    against it (rather than e.g. hour == SWEEP_HOUR) makes this safe to call
    every --interval-seconds tick without double-firing or depending on the
    loop landing on an exact minute."""
    if now_local.hour < SWEEP_HOUR:
        return None
    today = now_local.date()
    if last_sweep_date == today:
        return None
    return today


def _initial_last_sweep_date(now_local: datetime):
    """What `last_sweep_date` should start as when --loop boots, so a fresh
    start doesn't treat the sweep as immediately due.

    `last_sweep_date` lives only in memory, so every restart forgets whether
    today's sweep already ran. Naively initialising it to None makes a
    restart after SWEEP_HOUR fire an (idempotent but wasteful) sweep right
    away -- and under a crash-restart loop, repeats it indefinitely. If
    we've already passed SWEEP_HOUR local for today, assume today's sweep
    is spoken for and defer to tomorrow's; if we haven't reached SWEEP_HOUR
    yet, leave it None so a sweep that is genuinely still pending today
    fires normally once the clock reaches SWEEP_HOUR (see sweep_due)."""
    return now_local.date() if now_local.hour >= SWEEP_HOUR else None


def run_sweep(client: InfluxDBClient, sweep_date) -> None:
    """Re-backfill through `now`, from SWEEP_LOOKBACK_DAYS before `sweep_date`
    -- covers the two completed Pacific days D-1 and D-2 as of the sweep
    firing, not just two UTC calendar days sharing their date digits with D.

    Passing end_date=now() (rather than relying on backfill()'s "yesterday"
    default) is the part that actually reaches the just-completed Pacific
    day's evening: backfill() alone always stops at UTC-yesterday, which is
    still up to ~8h short of Pacific midnight. See the SWEEP_LOOKBACK_DAYS
    module comment for the full PST/PDT arithmetic. Goes through the same
    `backfill()` day-batch path --backfill uses, so this is a pure re-run:
    idempotent overwrites, no new classification logic."""
    now = _now()
    start = datetime(sweep_date.year, sweep_date.month, sweep_date.day, tzinfo=timezone.utc) \
        - timedelta(days=SWEEP_LOOKBACK_DAYS)
    logger.info(f"Nightly self-heal sweep: re-backfilling from {start:%Y-%m-%d} "
                f"through now ({SWEEP_LOOKBACK_DAYS} completed Pacific days plus today so far)")
    backfill(client, start, end_date=now)


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
        intervals = classify_day(query_api, day_start)
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

    # try/finally, not a trailing close(): --loop never falls out of its while,
    # so a close() after the branches is dead code there. This way the socket is
    # released on Ctrl-C and on an unhandled error too.
    try:
        if args.backtest:
            logger.info(f"Backtest mode: last {args.days} days (no writes)")
            backtest(client, args.days, compare_baths=args.compare_baths)
        elif args.backfill:
            start = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            logger.info(f"Backfilling hvac_mode from {start:%Y-%m-%d}")
            backfill(client, start)
        elif args.loop:
            logger.info(f"Loop mode: classifying every {args.interval}s, "
                        f"nightly self-heal sweep at {SWEEP_HOUR:02d}:00 local")
            last_sweep_date = _initial_last_sweep_date(_local_now())
            while True:
                try:
                    normal_run(client)
                except Exception as e:
                    logger.error(f"Classification pass failed: {e}")
                # Same process, same thread as normal_run above: the sweep
                # can never run concurrently with a --loop pass, only ever
                # between them -- no locking needed to avoid the wasted
                # duplicate work a truly concurrent backfill would do.
                due = sweep_due(_local_now(), last_sweep_date)
                if due is not None:
                    try:
                        run_sweep(client, due)
                        last_sweep_date = due
                    except Exception as e:
                        logger.error(f"Nightly sweep failed: {e}")
                time.sleep(args.interval)
        else:
            normal_run(client)
    finally:
        client.close()


if __name__ == "__main__":
    main()
