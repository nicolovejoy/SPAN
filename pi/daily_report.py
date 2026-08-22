#!/usr/bin/env python3
"""Daily energy report — queries InfluxDB, sends HTML email via Resend."""

import argparse
import base64
import calendar
import io
import json
import os
import re
import time
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from influxdb_client import InfluxDBClient

from rates import ENERGY_RATE, BASE_CHARGE_DAILY

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "home")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "span")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
REPORT_EMAIL = os.getenv("REPORT_EMAIL")
REPORT_FROM = os.getenv("REPORT_FROM", "SPAN Monitor <energy@span.pianohouseproject.org>")
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "7"))
LOCAL_TZ_NAME = os.getenv("TZ", "America/Los_Angeles")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

# Banner + subject-prefix when the Auxiliary/Heat Pump circuit's draw for the
# report day costs at least this much. Cost (not kWh) because that circuit also
# draws during cooling — small amounts are normal noise; only flag real spend.
AUX_HEAT_ALARM_USD = float(os.getenv("AUX_HEAT_ALARM_USD", "0.50"))
AUX_CIRCUIT_PATTERN = re.compile(r"Auxiliary", re.IGNORECASE)

# EV charging is a single dedicated circuit. Pin EV accounting to its exact name
# (same CHARGE_CIRCUIT env charge_detector uses) instead of the fuzzy Car regex
# — exact, one source of truth, no false positives.
EV_CIRCUIT = os.getenv("CHARGE_CIRCUIT", "Outdoor / Tesla Car Charger")


def ev_name_filter() -> str:
    """Flux `=~` regex matching the EV charger circuit name exactly. Anchored,
    with regex metachars + `/` (the /.../ literal delimiter) escaped. Spaces stay
    literal — RE2 rejects escaped spaces, so don't use re.escape here."""
    specials = set(r".^$*+?()[]{}|\/")
    escaped = "".join("\\" + c if c in specials else c for c in EV_CIRCUIT)
    return "^" + escaped + "$"


def flux_ts(dt: datetime) -> str:
    """Format datetime as Flux-compatible UTC timestamp."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day_utc_range(d: date) -> tuple[datetime, datetime]:
    """Convert a local date to UTC start/end datetimes."""
    start = datetime(d.year, d.month, d.day, tzinfo=LOCAL_TZ)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# ---------- circuit source routing (issue #9) ----------
#
# The collector writes raw 30s `circuit`/`power_w`; InfluxDB tasks derive
# `circuit_5m` / `circuit_1h`, each bucket carrying `energy_wh` (integral of
# |power_w| over the bucket, in Wh) and `power_w_mean`. Summing energy_wh costs
# one row per bucket instead of 120 raw points per hour — that's the whole win.
#
# Every circuit query below goes through _run_segments(), which splits the
# requested window into (measurement, start, stop) segments: raw for anything
# older than the backfill reaches, the rollup for the stretch it covers, raw
# again for the fresh tail the rollup task hasn't written yet. Cuts land on the
# aggregation grid so no output window straddles a segment boundary, and with no
# rollups present at all the plan degrades to a single raw segment — i.e. exactly
# the queries this file ran before #9. Panel queries have no rollup and are
# untouched.

MEAS_RAW = "circuit"
MEAS_5M = "circuit_5m"
MEAS_1H = "circuit_1h"
ROLLUP_PERIOD = {MEAS_5M: timedelta(minutes=5), MEAS_1H: timedelta(hours=1)}
ROLLUP_EVERY = {MEAS_5M: "5m", MEAS_1H: "1h"}

# Kill switch — USE_ROLLUPS=0 forces the pre-#9 raw queries.
USE_ROLLUPS = os.getenv("USE_ROLLUPS", "1").lower() not in ("0", "false", "no")
# Which end of its bucket a rollup point is stamped at. Auto-detected per run;
# this only settles a tie (aggregateWindow's own default is the window stop).
ROLLUP_STAMP = os.getenv("ROLLUP_STAMP", "stop")

_EVERY_RE = re.compile(r"^(\d+)(m|h)$")
_SUM_TAIL = '  |> group(columns: ["_time"])\n  |> sum()\n'


def _parse_flux_ts(s: str) -> datetime:
    """Inverse of flux_ts()."""
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _shift_ts(s: str, delta: timedelta) -> str:
    return flux_ts(_parse_flux_ts(s) + delta)


def _flux_dur(delta: timedelta) -> str:
    return f"{int(delta.total_seconds())}s"


def _every_minutes(every: str | None) -> int | None:
    """Minutes in a sub-day `every` literal ("15m", "2h"). None for 1d/1mo."""
    m = _EVERY_RE.match(every) if every else None
    return int(m.group(1)) * (60 if m.group(2) == "h" else 1) if m else None


def _grid_floor(dt: datetime, every: str | None) -> datetime:
    """Largest aggregateWindow boundary <= dt. Sub-day windows are anchored to
    the Unix epoch (verified against Influx); 1d/1mo follow the local calendar,
    matching `option location`."""
    if every is None:
        return dt
    mins = _every_minutes(every)
    if mins:
        step = mins * 60
        return datetime.fromtimestamp(int(dt.timestamp()) // step * step, tz=timezone.utc)
    lt = dt.astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if every == "1mo":
        lt = lt.replace(day=1)
    return lt.astimezone(timezone.utc)


def _grid_ceil(dt: datetime, every: str | None) -> datetime:
    """Smallest aggregateWindow boundary >= dt."""
    floor = _grid_floor(dt, every)
    if every is None or floor == dt:
        return floor
    mins = _every_minutes(every)
    if mins:
        return floor + timedelta(minutes=mins)
    lt = floor.astimezone(LOCAL_TZ)
    if every == "1mo":
        y, m = add_months(lt.year, lt.month, 1)
        lt = lt.replace(year=y, month=m)
    else:
        lt = lt + timedelta(days=1)   # wall-clock, so DST days stay whole
    return lt.astimezone(timezone.utc)


def _rollup_src(every: str | None) -> str:
    """Coarsest rollup whose buckets tile `every` exactly."""
    mins = _every_minutes(every)
    if mins is not None and mins < 60:
        return MEAS_5M if mins % 5 == 0 else MEAS_RAW
    return MEAS_1H


_ROLLUP_SPAN: dict[str, tuple[datetime, datetime] | None] = {}
_ROLLUP_STAMP_AT: dict[str, str] = {MEAS_RAW: "stop"}   # raw ignores it


def rollup_span(query_api, src: str) -> tuple[datetime, datetime] | None:
    """(oldest, newest) timestamp in rollup measurement `src`, or None when it
    holds nothing yet (tasks/backfill not run). Probed once per process; it only
    touches one row per bucket, so it is cheap even unbounded."""
    if src in _ROLLUP_SPAN:
        return _ROLLUP_SPAN[src]
    flux = f'''
base = from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "{src}" and r._field == "energy_wh")
  |> keep(columns: ["_time"])
  |> group()

union(tables: [base |> min(column: "_time"), base |> max(column: "_time")])
'''
    span = None
    try:
        times = sorted(rec.get_time()
                       for table in query_api.query(flux, org=INFLUXDB_ORG)
                       for rec in table.records)
        if times:
            span = (times[0], times[-1])
    except Exception as e:   # an empty measurement is fine; a broken query is not
        logger.warning(f"rollup probe for {src} failed, staying on raw: {e}")
    _ROLLUP_SPAN[src] = span
    logger.info(f"rollup {src}: {'absent' if span is None else f'{span[0]} .. {span[1]}'}")
    return span


def _rollup_stamp(query_api, src: str) -> str:
    """"stop" or "start" — which end of its bucket a rollup point is stamped at.
    aggregateWindow stamps the stop by default, but a task written with
    timeSrc: "_start" does the opposite and guessing wrong shifts every window by
    a whole bucket. Detected once by matching rollup buckets against raw energy
    over a recent 6h window; falls back to ROLLUP_STAMP when the window is too
    quiet to tell the two apart."""
    if src in _ROLLUP_STAMP_AT:
        return _ROLLUP_STAMP_AT[src]
    stamp = ROLLUP_STAMP
    period, every = ROLLUP_PERIOD[src], ROLLUP_EVERY[src]
    span = rollup_span(query_api, src)
    if span is None:
        return stamp
    try:
        end = _grid_floor(span[1], "1h") - period      # one bucket of slack
        begin = max(end - timedelta(hours=6), _grid_ceil(span[0], "1h"))
        if begin < end:
            raw = dict(_summed_rows(query_api, MEAS_RAW, flux_ts(begin), flux_ts(end), every))
            # rollup buckets as stored, unshifted — stamps either end of [begin, end)
            probe = f'''from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {flux_ts(begin)}, stop: {flux_ts(end + period)})
  |> filter(fn: (r) => r._measurement == "{src}" and r._field == "energy_wh")
  |> filter(fn: (r) => exists r._value)
  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
''' + _SUM_TAIL
            roll = [(rec.get_time(), rec.get_value() or 0.0)
                    for table in query_api.query(probe, org=INFLUXDB_ORG)
                    for rec in table.records]
            energy = sum(raw.values())
            if raw and roll and energy > 0:
                err = {
                    # stop-stamped: point at t is the window ending at t
                    "stop": sum(abs(v - raw[t]) for t, v in roll if t in raw),
                    # start-stamped: point at t is the window ending at t + period
                    "start": sum(abs(v - raw[t + period]) for t, v in roll
                                 if t + period in raw),
                }
                best = min(err, key=err.get)
                if abs(err["stop"] - err["start"]) > 0.01 * energy:
                    stamp = best
                else:
                    logger.warning(f"{src} stamp detection ambiguous "
                                   f"({err}); assuming {stamp}")
    except Exception as e:
        logger.warning(f"{src} stamp detection failed, assuming {stamp}: {e}")
    _ROLLUP_STAMP_AT[src] = stamp
    logger.info(f"rollup {src} stamped at bucket {stamp}")
    return stamp


def _circuit_kwh_flux(src: str, start: str, stop: str, every: str | None,
                      name_filter: str | None = None, mode: str = "energy",
                      stamp: str = "stop") -> str:
    """Pipeline yielding circuit energy in kWh — one value per `every`-window, or
    one per series when `every` is None. Raw integrates 30s power exactly as
    before #9; a rollup just sums its precomputed energy_wh. `mode="mean"`
    reproduces the mean-power-times-window form the hourly query has always used.

    Rollup rows are re-stamped to their bucket midpoint (and the range shifted to
    match) so a bucket can never land in the neighbouring output window."""
    raw = src == MEAS_RAW
    field = "power_w" if raw else ("energy_wh" if mode == "energy" else "power_w_mean")
    nf = f'  |> filter(fn: (r) => r.name =~ /(?i){name_filter}/)\n' if name_filter else ''
    # rollup buckets can be null where the source had no points; raw never is
    nn = '' if raw else '  |> filter(fn: (r) => exists r._value)\n'
    shift = ''
    if raw:
        rng_start, rng_stop = start, stop
    else:
        period = ROLLUP_PERIOD[src]
        mid = -period / 2 if stamp == "stop" else period / 2
        offset = period / 2 - mid          # timedelta(0) or one period
        rng_start, rng_stop = _shift_ts(start, offset), _shift_ts(stop, offset)
        if every:
            shift = f'  |> timeShift(duration: {_flux_dur(mid)})\n'

    if every is None:
        agg = '  |> integral(unit: 1h)\n' if raw else '  |> sum()\n'
    elif mode == "mean":
        agg = f'  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)\n'
    elif raw:
        agg = (f'  |> aggregateWindow(\n'
               f'       every: {every},\n'
               f'       fn: (column, tables=<-) => tables |> integral(unit: 1h, column: column),\n'
               f'       createEmpty: false)\n')
    else:
        agg = f'  |> aggregateWindow(every: {every}, fn: sum, createEmpty: false)\n'

    loc = (f'import "timezone"\noption location = timezone.location(name: "{LOCAL_TZ_NAME}")\n\n'
           if every in ("1d", "1mo") else '')
    return f'''{loc}from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {rng_start}, stop: {rng_stop})
  |> filter(fn: (r) => r._measurement == "{src}" and r._field == "{field}")
{nf}{nn}{shift}  |> map(fn: (r) => ({{r with _value: if r._value < 0.0 then -r._value else r._value}}))
{agg}  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
'''


def _circuit_records(query_api, src: str, start: str, stop: str, every: str | None,
                     name_filter: str | None = None, mode: str = "energy",
                     tail: str = "") -> list:
    """Run one segment's circuit query and return its Flux records."""
    flux = _circuit_kwh_flux(src, start, stop, every, name_filter, mode,
                             _rollup_stamp(query_api, src)) + tail
    return [rec for table in query_api.query(flux, org=INFLUXDB_ORG)
            for rec in table.records]


def _summed_rows(query_api, src: str, start: str, stop: str, every: str | None,
                 name_filter: str | None = None,
                 mode: str = "energy") -> list[tuple[datetime, float]]:
    """(utc_window_stop, kWh) summed across every circuit matching name_filter."""
    return [(rec.get_time(), rec.get_value() or 0.0)
            for rec in _circuit_records(query_api, src, start, stop, every,
                                        name_filter, mode, tail=_SUM_TAIL)]


def _circuit_segments(query_api, start: str, stop: str,
                      every: str | None) -> list[tuple[str, str, str]]:
    """(measurement, start, stop) segments tiling [start, stop) — see the note at
    the top of this section. Always at least one segment; a lone raw one whenever
    the rollup can't help."""
    src = _rollup_src(every)
    span = rollup_span(query_api, src) if USE_ROLLUPS and src != MEAS_RAW else None
    if span is None:
        return [(MEAS_RAW, start, stop)]
    start_dt, stop_dt = _parse_flux_ts(start), _parse_flux_ts(stop)
    # Snap the rollup segment inwards to whole aggregation windows: conservative
    # under either stamping convention, and it keeps every truncated edge window
    # (whose stamp is clamped to the range bound) on the raw side, where it
    # stamps exactly as it always has.
    lo = _grid_ceil(max(start_dt, span[0]), every)
    hi = _grid_floor(min(stop_dt, span[1]), every)
    if hi <= lo:
        return [(MEAS_RAW, start, stop)]
    segments = [(src, flux_ts(lo), flux_ts(hi))]
    if start_dt < lo:
        segments.insert(0, (MEAS_RAW, start, flux_ts(lo)))
    if hi < stop_dt:
        segments.append((MEAS_RAW, flux_ts(hi), stop))
    return segments


def _run_segments(query_api, start: str, stop: str, every: str | None, run) -> list:
    """Concatenate run(measurement, start, stop) over each segment. A rollup
    segment that comes back empty is re-run against raw: a slow report beats one
    full of zeros."""
    rows: list = []
    for src, seg_start, seg_stop in _circuit_segments(query_api, start, stop, every):
        part = run(src, seg_start, seg_stop)
        if not part and src != MEAS_RAW:
            logger.warning(f"{src} empty over {seg_start}..{seg_stop}; falling back to raw")
            part = run(MEAS_RAW, seg_start, seg_stop)
        rows.extend(part)
    return rows


# ---------- weekly report + anomaly check: energy_wh_counter query layer ----------
#
# Deliberately narrower than the #9 segment router above: the weekly briefing and
# the daily anomaly check both read circuit_1h.energy_wh_counter only, never raw
# and never circuit_5m (design doc "Data source"). Every window this code asks
# for ends at least a day in the past, well past circuit_1h's 5-65 min tail lag,
# so there is no fresh-tail case to handle and no raw fallback is needed.
# circuit_1h is stop-stamped (verified invariant, influx_tasks/README.md) — that
# is hard-coded below rather than detected at runtime.

COUNTER_FIELD = "energy_wh_counter"


def _counter_kwh_flux(start: str, stop: str, every: str,
                      name_filter: str | None = None) -> str:
    """circuit_1h.energy_wh_counter summed into `every`-windows, in kWh.
    Pacific-aligned for 1d/1mo grids. Re-centred from its stop stamp to the
    bucket midpoint (shift range forward one period, then timeShift back half a
    period) so a bucket can never land in the neighbouring output window —
    the same recentring _circuit_kwh_flux does for stamp="stop"."""
    period = ROLLUP_PERIOD[MEAS_1H]
    mid = -period / 2
    rng_start, rng_stop = _shift_ts(start, period), _shift_ts(stop, period)
    nf = f'  |> filter(fn: (r) => r.name =~ /(?i){name_filter}/)\n' if name_filter else ''
    loc = (f'import "timezone"\noption location = timezone.location(name: "{LOCAL_TZ_NAME}")\n\n'
           if every in ("1d", "1mo") else '')
    return f'''{loc}from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {rng_start}, stop: {rng_stop})
  |> filter(fn: (r) => r._measurement == "{MEAS_1H}" and r._field == "{COUNTER_FIELD}")
  |> filter(fn: (r) => exists r._value)
{nf}  |> timeShift(duration: {_flux_dur(mid)})
  |> aggregateWindow(every: {every}, fn: sum, createEmpty: false)
  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
'''


def query_daily_circuit_counter_kwh(query_api, start: str, stop: str) -> list[tuple[str, date, float]]:
    """(circuit_name, local_date, kwh) via circuit_1h.energy_wh_counter — one row
    per circuit per Pacific day covered by [start, stop). The single workhorse
    query for the weekly briefing and the anomaly check: everything else (week
    totals, month totals, category rollups) is derived from these rows in pure
    Python — see the grouping helpers below."""
    flux = _counter_kwh_flux(start, stop, "1d")
    out: list[tuple[str, date, float]] = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for rec in table.records:
            out.append((rec.values.get("name", "Unknown"), _local_day(rec.get_time()),
                       rec.get_value() or 0.0))
    return out


# ---------- Task 2: Pure date/grouping helpers ----------


def local_week_start(d: date) -> date:
    """Monday on or before `d` — report weeks run Monday-Sunday."""
    return d - timedelta(days=d.weekday())


def category_day_kwh(rows: list[tuple[str, date, float]]) -> dict[date, dict[str, float]]:
    """rows -> {local_date: {category: kwh}}, circuits rolled up via display_bucket."""
    out: dict[date, dict[str, float]] = {}
    for name, day, kwh in rows:
        cat = display_bucket(name)
        day_map = out.setdefault(day, {})
        day_map[cat] = day_map.get(cat, 0.0) + kwh
    return out


def week_totals(day_cat: dict[date, dict[str, float]], week_start: date) -> dict[str, float]:
    """Sum category kWh over [week_start, week_start+7)."""
    week_end = week_start + timedelta(days=7)
    out: dict[str, float] = {}
    for day, cats in day_cat.items():
        if week_start <= day < week_end:
            for cat, kwh in cats.items():
                out[cat] = out.get(cat, 0.0) + kwh
    return out


def circuit_week_totals(rows: list[tuple[str, date, float]], week_start: date) -> dict[str, float]:
    """Per-circuit (not per-category) kWh over [week_start, week_start+7)."""
    week_end = week_start + timedelta(days=7)
    out: dict[str, float] = {}
    for name, day, kwh in rows:
        if week_start <= day < week_end:
            out[name] = out.get(name, 0.0) + kwh
    return out


def category_top_circuits(rows: list[tuple[str, date, float]], week_start: date,
                          category: str, n: int = 5) -> list[tuple[str, float]]:
    """Top-n circuits by kWh within `category`, for the usage table's nested rows."""
    totals = circuit_week_totals(rows, week_start)
    names = [name for name in totals if display_bucket(name) == category]
    return sorted(((name, totals[name]) for name in names), key=lambda x: -x[1])[:n]


def trailing_week_starts(target_week_start: date, n: int) -> list[date]:
    """The n Mondays strictly before target_week_start, oldest first."""
    return [target_week_start - timedelta(days=7 * i) for i in range(n, 0, -1)]


def _sum_days(daily: dict[date, float], lo: date, hi_exclusive: date) -> float:
    return sum(v for d, v in daily.items() if lo <= d < hi_exclusive)


def unmonitored_week_kwh(panel_week_kwh: float, circuit_totals: dict[str, float]) -> float:
    """Panel total minus every known circuit — the energy the panel meters but no
    circuit sensor does (no washer/dryer/water-heater circuit; see #17). Floored
    at zero: circuit-level counter noise can occasionally exceed a noisy panel
    integral over a short window, and a negative "unmonitored" number is never
    meaningful."""
    return max(0.0, panel_week_kwh - sum(circuit_totals.values()))


def _all_categories() -> list[str]:
    """Category display order: categories.json rules, then its default bucket."""
    rules, default = _load_bucket_rules()
    return [c for c, _ in rules] + [default]


def _merge_keyed(rows) -> list[tuple]:
    """Sum values sharing a key, ascending by key. Segment cuts land on window
    boundaries so collisions shouldn't arise — but sum rather than silently drop
    a day if one ever does."""
    acc: dict = {}
    for k, v in rows:
        acc[k] = acc.get(k, 0.0) + v
    return sorted(acc.items())


def _local_day(t: datetime) -> date:
    # aggregateWindow stamps each window at its STOP, i.e. local midnight of the next day
    return (t.astimezone(LOCAL_TZ) - timedelta(seconds=1)).date()


def query_total_kwh(query_api, start: str, stop: str) -> float:
    """Total grid consumption in kWh for the given range."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "panel" and r._field == "grid_power_w")
  |> integral(unit: 1h)
  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
'''
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            return round(record.get_value(), 2)
    return 0.0


_BY_NAME_TAIL = '''  |> group(columns: ["name"])
  |> sum(column: "_value")
  |> group()
  |> keep(columns: ["name", "_value"])
'''


def query_circuit_energy(query_api, start: str, stop: str) -> list[dict]:
    """Energy per circuit in kWh, summed across tag-variants, sorted descending."""
    def run(src, seg_start, seg_stop):
        return [(rec.values.get("name", "Unknown"), rec.get_value() or 0.0)
                for rec in _circuit_records(query_api, src, seg_start, seg_stop,
                                            every=None, tail=_BY_NAME_TAIL)]

    totals: dict[str, float] = {}
    for name, kwh in _run_segments(query_api, start, stop, None, run):
        totals[name] = totals.get(name, 0.0) + kwh
    return sorted(({"name": n, "kwh": round(k, 2)} for n, k in totals.items()),
                  key=lambda c: -c["kwh"])


_BUCKET_RULES: list[tuple[str, re.Pattern]] | None = None
_BUCKET_DEFAULT: str = "Else"

_FALLBACK_RULES = [
    ("Lights",     r"Light"),
    ("HVAC",       r"Heat pump|Auxiliary"),
    ("Car",        r"Tesla|Car Charger|\bEV\b"),
    ("Appliances", r"Kitchen|Oven|Dishwasher|Refrigerator|Fridge|Microwave|Range|Washer|Dryer|Laundry|Beverage|Freezer"),
]


def _load_bucket_rules() -> tuple[list[tuple[str, re.Pattern]], str]:
    """Load (compiled) bucket rules from categories.json, with a fallback."""
    global _BUCKET_RULES, _BUCKET_DEFAULT
    if _BUCKET_RULES is not None:
        return _BUCKET_RULES, _BUCKET_DEFAULT
    cats_path = Path(__file__).parent / "categories.json"
    try:
        cfg = json.loads(cats_path.read_text())
        _BUCKET_DEFAULT = cfg.get("default", "Else")
        _BUCKET_RULES = [
            (r["category"], re.compile(r["pattern"], re.IGNORECASE))
            for r in cfg.get("rules", [])
        ]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"categories.json unavailable, using fallback buckets: {e}")
        _BUCKET_RULES = [(c, re.compile(p, re.IGNORECASE)) for c, p in _FALLBACK_RULES]
    return _BUCKET_RULES, _BUCKET_DEFAULT


def query_circuit_kwh_by_name(query_api, start: str, stop: str) -> dict[str, float]:
    """{circuit_name: kWh} over [start, stop). Reuses query_circuit_energy shape."""
    return {c["name"]: c["kwh"] for c in query_circuit_energy(query_api, start, stop)}


def query_hourly_circuit_kwh(query_api, start: str, stop: str,
                             name_filter: str) -> list[tuple[datetime, float]]:
    """Hourly kWh summed across circuits matching name_filter regex (case-insensitive).

    Kept on mean-power × 1h (rollup field power_w_mean) rather than switching to
    energy_wh, so the numbers this feeds — today/week EV totals — don't move."""
    def run(src, seg_start, seg_stop):
        return _summed_rows(query_api, src, seg_start, seg_stop, "1h",
                            name_filter, mode="mean")

    return _merge_keyed(_run_segments(query_api, start, stop, "1h", run))


def query_interval_panel_kwh(query_api, start: str, stop: str,
                             every: str) -> list[tuple[datetime, float]]:
    """Grid kWh per `every`-window (integral, exact). Stop-stamped UTC times."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "panel" and r._field == "grid_power_w")
  |> aggregateWindow(
       every: {every},
       fn: (column, tables=<-) => tables |> integral(unit: 1h, column: column),
       createEmpty: true)
  |> map(fn: (r) => ({{r with _value: (if exists r._value then r._value else 0.0) / 1000.0}}))
'''
    out: list[tuple[datetime, float]] = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            out.append((record.get_time(), record.get_value() or 0.0))
    return out


def query_interval_circuit_kwh(query_api, start: str, stop: str, every: str,
                               name_filter: str | None = None) -> list[tuple[str, datetime, float]]:
    """Per-circuit kWh per `every`-window (integral). Returns (name, utc_stop, kwh)."""
    def run(src, seg_start, seg_stop):
        return [(rec.values.get("name", "Unknown"), rec.get_time(), rec.get_value() or 0.0)
                for rec in _circuit_records(query_api, src, seg_start, seg_stop,
                                            every, name_filter)]

    # callers accumulate additively, so segments just concatenate
    return _run_segments(query_api, start, stop, every, run)


def query_interval_circuit_kwh_summed(query_api, start: str, stop: str, every: str,
                                      name_filter: str) -> list[tuple[datetime, float]]:
    """Per-`every`-window kWh summed across circuits matching name_filter.
    One (utc_stop, kwh) per window; same bucketing as query_interval_panel_kwh."""
    def run(src, seg_start, seg_stop):
        return _summed_rows(query_api, src, seg_start, seg_stop, every, name_filter)

    return _merge_keyed(_run_segments(query_api, start, stop, every, run))


def query_daily_panel_kwh(query_api, start: str, stop: str) -> list[tuple[date, float]]:
    """Daily grid kWh via per-local-day integral. One record per local calendar day."""
    flux = f'''
import "timezone"
option location = timezone.location(name: "{LOCAL_TZ_NAME}")

from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "panel" and r._field == "grid_power_w")
  |> aggregateWindow(
       every: 1d,
       fn: (column, tables=<-) => tables |> integral(unit: 1h, column: column),
       createEmpty: true)
  |> map(fn: (r) => ({{r with _value: (if exists r._value then r._value else 0.0) / 1000.0}}))
'''
    out: list[tuple[date, float]] = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            t = record.get_time().astimezone(LOCAL_TZ)
            # aggregateWindow stamps each window at its STOP, i.e. local midnight of the next day
            day = (t - timedelta(seconds=1)).date()
            out.append((day, record.get_value() or 0.0))
    return out


def query_daily_circuit_kwh(query_api, start: str, stop: str,
                            name_filter: str) -> list[tuple[date, float]]:
    """Daily kWh summed across circuits matching name_filter (case-insensitive)."""
    def run(src, seg_start, seg_stop):
        return _summed_rows(query_api, src, seg_start, seg_stop, "1d", name_filter)

    return _merge_keyed((_local_day(t), v)
                        for t, v in _run_segments(query_api, start, stop, "1d", run))


def query_monthly_panel_kwh(query_api, start: str, stop: str) -> list[tuple[tuple[int, int], float]]:
    """Monthly grid kWh via per-local-month integral. One record per local calendar month."""
    flux = f'''
import "timezone"
option location = timezone.location(name: "{LOCAL_TZ_NAME}")

from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "panel" and r._field == "grid_power_w")
  |> aggregateWindow(
       every: 1mo,
       fn: (column, tables=<-) => tables |> integral(unit: 1h, column: column),
       createEmpty: true)
  |> map(fn: (r) => ({{r with _value: (if exists r._value then r._value else 0.0) / 1000.0}}))
'''
    out: list[tuple[tuple[int, int], float]] = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            t = record.get_time().astimezone(LOCAL_TZ)
            d = (t - timedelta(seconds=1)).date()
            out.append(((d.year, d.month), record.get_value() or 0.0))
    return out


def query_monthly_circuit_kwh(query_api, start: str, stop: str,
                              name_filter: str) -> list[tuple[tuple[int, int], float]]:
    """Monthly kWh summed across circuits matching name_filter (case-insensitive)."""
    def run(src, seg_start, seg_stop):
        return _summed_rows(query_api, src, seg_start, seg_stop, "1mo", name_filter)

    rows = _run_segments(query_api, start, stop, "1mo", run)
    return _merge_keyed(((_local_day(t).year, _local_day(t).month), v) for t, v in rows)


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Add `delta` calendar months to (year, month). Handles negative deltas."""
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def latest_complete_month(target_date: date) -> tuple[int, int]:
    """Most recent month fully covered by target_date."""
    last_day = calendar.monthrange(target_date.year, target_date.month)[1]
    if target_date.day == last_day:
        return target_date.year, target_date.month
    return add_months(target_date.year, target_date.month, -1)


# Per-category line colors (keys = categories.json buckets). Total/avg/aux fixed.
CATEGORY_COLORS = {
    "Lights": "#f1c40f",
    "HVAC": "#e74c3c",
    "Car": "#3498db",
    "Appliances": "#e67e22",
    "Else": "#16a085",
}
_FALLBACK_CYCLE = ["#8e44ad", "#2980b9", "#27ae60", "#d35896"]


def build_today_series(query_api, today_start: str, today_end: str,
                       week_start: str, aux_alarm: bool, every: str = "15m") -> dict:
    """Assemble the today line-chart data: total + top-3 category lines +
    dotted 7-day same-slot total average (+ aux-heat line if alarming).

    All series are aligned on the canonical bucket grid (x = local bucket start)."""
    every_min = 15
    every_td = timedelta(minutes=every_min)
    total = query_interval_panel_kwh(query_api, today_start, today_end, every)
    if not total:
        return {"times": []}

    stops = [t for t, _ in total]               # UTC stop stamps, canonical order
    idx = {t: i for i, t in enumerate(stops)}
    n = len(stops)
    times = [t.astimezone(LOCAL_TZ) - every_td for t in stops]   # x = bucket start (local)
    total_vals = [v for _, v in total]

    # Per-circuit → roll up to category lines; pick top 3 by today's total.
    cat_series: dict[str, list[float]] = {}
    cat_total: dict[str, float] = {}
    for name, t, v in query_interval_circuit_kwh(query_api, today_start, today_end, every):
        i = idx.get(t)
        if i is None:
            continue
        cat = display_bucket(name)
        cat_series.setdefault(cat, [0.0] * n)[i] += v
        cat_total[cat] = cat_total.get(cat, 0.0) + v
    top3 = sorted(cat_total, key=lambda c: -cat_total[c])[:3]
    cats = [(c, cat_series[c]) for c in top3]

    # Dotted 7-day average of total, by 15-min slot-of-day.
    slot_vals: dict[int, list[float]] = {}
    for t, v in query_interval_panel_kwh(query_api, week_start, today_end, every):
        st = t.astimezone(LOCAL_TZ) - every_td
        slot_vals.setdefault(st.hour * 4 + st.minute // 15, []).append(v)
    slot_avg = {s: sum(xs) / len(xs) for s, xs in slot_vals.items()}
    avg_total = [slot_avg.get(lt.hour * 4 + lt.minute // 15, 0.0) for lt in times]

    series = {"times": times, "total": total_vals, "cats": cats, "avg_total": avg_total}

    if aux_alarm:
        aux = [0.0] * n
        for _, t, v in query_interval_circuit_kwh(query_api, today_start, today_end,
                                                  every, name_filter="Auxiliary"):
            i = idx.get(t)
            if i is not None:
                aux[i] += v
        series["aux"] = aux
    return series


def build_week_series(query_api, start: str, today_end: str,
                      target_date: date, every: str = "2h") -> dict:
    """Last-7-days load (EV-excluded) at 2h grain vs same-weekday+slot averages
    over the trailing 5- and 12-week windows. EV (the dedicated charger circuit)
    is subtracted per bucket. Also returns weekly-average EV (5/12-week, complete
    weeks only) for the EV callout. `start` must cover ≥12 complete prior weeks."""
    every_td = timedelta(hours=2)
    panel = query_interval_panel_kwh(query_api, start, today_end, every)
    if not panel:
        return {"times": []}
    ev_bkt = dict(query_interval_circuit_kwh_summed(
        query_api, start, today_end, every, ev_name_filter()))
    excl = [(t, max(0.0, v - ev_bkt.get(t, 0.0))) for t, v in panel]

    def slot_avg(since_day: date) -> dict[tuple[int, int], float]:
        cut = local_day_utc_range(since_day)[0].astimezone(LOCAL_TZ)
        acc: dict[tuple[int, int], list[float]] = {}
        for t, v in excl:
            st = t.astimezone(LOCAL_TZ) - every_td
            if st >= cut:
                acc.setdefault((st.weekday(), st.hour // 2), []).append(v)
        return {k: sum(xs) / len(xs) for k, xs in acc.items()}

    avg5 = slot_avg(target_date - timedelta(days=34))
    avg12 = slot_avg(target_date - timedelta(days=83))

    cutoff = local_day_utc_range(target_date - timedelta(days=6))[0].astimezone(LOCAL_TZ)
    times, actual, roll5, roll12 = [], [], [], []
    for t, v in excl:
        st = t.astimezone(LOCAL_TZ) - every_td
        if st < cutoff:
            continue
        key = (st.weekday(), st.hour // 2)
        times.append(st)
        actual.append(v)
        roll5.append(avg5.get(key, 0.0))
        roll12.append(avg12.get(key, 0.0))

    # Weekly-average EV over complete prior weeks (excludes the in-progress week).
    daily_ev = dict(query_daily_circuit_kwh(query_api, start, today_end, ev_name_filter()))

    def ev_week_avg(weeks: int) -> float:
        hi = target_date - timedelta(days=7)            # last day before this week
        lo = target_date - timedelta(days=6 + 7 * weeks)  # oldest counted day
        return sum(v for d, v in daily_ev.items() if lo <= d <= hi) / weeks

    return {
        "times": times, "actual": actual, "avg5": roll5, "avg12": roll12,
        "ev_avg5": ev_week_avg(5), "ev_avg12": ev_week_avg(12),
    }


def render_today_chart(series: dict) -> str:
    """Total + dotted 7-day avg + top-3 category lines (+ aux heat) at 15-min grain."""
    times = series.get("times")
    if not times:
        return ""
    fig, ax = plt.subplots(figsize=(7, 3.4), dpi=120)
    if series.get("avg_total"):
        ax.plot(times, series["avg_total"], color="#7f8c8d", linewidth=1.4,
                linestyle=":", label="Total · 7-day avg")
    ax.plot(times, series["total"], color="#2c3e50", linewidth=2.2, label="Total", zorder=5)
    fb = iter(_FALLBACK_CYCLE)
    for name, vals in series.get("cats", []):
        ax.plot(times, vals, linewidth=1.3,
                color=CATEGORY_COLORS.get(name, next(fb, "#888")), label=name)
    if series.get("aux"):
        ax.plot(times, series["aux"], color="#c0392b", linewidth=1.4,
                linestyle="--", label="Aux heat")
    ax.set_xlabel("Time of day (PST)")
    ax.set_ylabel("kWh per 15 min")
    ax.set_ylim(bottom=0)
    ax.set_xlim(times[0], times[-1])
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=8, ncol=2)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-I %p", tz=LOCAL_TZ))
    # Pin ticks to fixed clock hours (0,3,6,…) so labels land on midnight, not the view edge.
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3), tz=LOCAL_TZ))
    fig.autofmt_xdate()
    fig.tight_layout()
    return _fig_to_b64(fig)


def render_week_compare(series: dict, avg_key: str, avg_label: str,
                        avg_color: str, avg_style: str) -> str:
    """This week's load (excl. car) vs one same-time average, 2-hour grain."""
    times = series.get("times")
    avg = series.get(avg_key)
    if not times or not avg:
        return ""
    fig, ax = plt.subplots(figsize=(7, 3.4), dpi=120)
    ax.plot(times, avg, color=avg_color, linewidth=1.4,
            linestyle=avg_style, label=avg_label)
    ax.plot(times, series["actual"], color="#3498db", linewidth=1.8,
            label="This week (excl. car)")
    ax.set_xlabel("Day (PST)")
    ax.set_ylabel("kWh per 2 h")
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %-m/%-d", tz=LOCAL_TZ))
    ax.xaxis.set_major_locator(mdates.DayLocator(tz=LOCAL_TZ))
    fig.autofmt_xdate()
    fig.tight_layout()
    return _fig_to_b64(fig)


def render_monthly_chart(monthly_excl: list[tuple[tuple[int, int], float]],
                         monthly_ev: dict[tuple[int, int], float]) -> str:
    """Stacked monthly bars (excl + EV) + dashed total-avg line."""
    if not monthly_excl:
        return ""

    labels = [f"{calendar.month_abbr[m]} '{str(y)[2:]}" for (y, m), _ in monthly_excl]
    excl_values = [v for _, v in monthly_excl]
    ev_values = [monthly_ev.get(ym, 0.0) for ym, _ in monthly_excl]

    x = list(range(len(excl_values)))
    fig, ax = plt.subplots(figsize=(7, 3.2), dpi=120)
    ax.bar(x, excl_values, width=0.7, color="#3498db", label="excl. car")
    ax.bar(x, ev_values, width=0.7, bottom=excl_values, color="#9b59b6", label="EV")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9, rotation=30, ha="right")
    ax.set_ylabel("kWh")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    return _fig_to_b64(fig)


def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def cost_n_days(kwh: float, days: int) -> float:
    """Total cost for `days` days at `kwh` energy (energy + N × base)."""
    return round(kwh * ENERGY_RATE + days * BASE_CHARGE_DAILY, 2)


def display_bucket(name: str) -> str:
    """Roll up raw circuit name into a coarse display bucket (categories.json)."""
    rules, default = _load_bucket_rules()
    for category, pat in rules:
        if pat.search(name):
            return category
    return default


def _aggregate_by_bucket(circuits: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for c in circuits:
        bucket = display_bucket(c["name"])
        out[bucket] = out.get(bucket, 0.0) + c["kwh"]
    return out


def merge_circuits(today_list: list[dict], week_list: list[dict],
                   n: int = 10) -> tuple[list[dict], dict]:
    """Top N display buckets by max(day, week/7) — surfaces consistent heavy
    users even on quiet days while still ranking today's spikes. Raw circuits
    are first aggregated into coarse buckets (see display_bucket).
    Returns (rows, totals)."""
    today_map = _aggregate_by_bucket(today_list)
    week_map = _aggregate_by_bucket(week_list)
    names = set(today_map) | set(week_map)
    rows = sorted(
        [{"name": name,
          "kwh_day": today_map.get(name, 0.0),
          "kwh_week": week_map.get(name, 0.0)} for name in names],
        key=lambda r: max(r["kwh_day"], r["kwh_week"] / 7.0),
        reverse=True,
    )[:n]
    totals = {
        "kwh_day": sum(r["kwh_day"] for r in rows),
        "kwh_week": sum(r["kwh_week"] for r in rows),
    }
    return rows, totals


def event_summary(events: list[dict]) -> dict:
    """Count + kWh + cost (recomputed at current ENERGY_RATE) for a list of events."""
    kwh = sum((e.get("energy_kwh") or 0) for e in events)
    return {"count": len(events), "kwh": kwh, "cost": round(kwh * ENERGY_RATE, 2)}


def query_events(query_api, measurement: str, start: str, stop: str) -> list[dict]:
    """Query pivoted event records (bath_event or charge_event)."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
'''
    results = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            results.append(record.values)
    return results


@dataclass
class Period:
    """Energy over a contiguous range. Both grid and EV in kWh."""
    grid: float = 0.0
    ev: float = 0.0
    days: int = 1

    @property
    def excl(self) -> float:
        return max(0.0, self.grid - self.ev)

    @property
    def cost(self) -> float:
        return round(self.grid * ENERGY_RATE + self.days * BASE_CHARGE_DAILY, 2)

    @property
    def ev_cost(self) -> float:
        return round(self.ev * ENERGY_RATE, 2)


@dataclass
class ReportContext:
    query_api: Any
    target_date: date
    force_monthly: bool
    today: Period
    week: Period
    prev_day_kwh: float
    daily_grid: dict[date, float]  # 5wk window
    daily_ev: dict[date, float]    # 5wk window
    today_series: dict             # today line chart (total + top-3 cats + 7d avg + aux)
    week_series: dict              # week line chart (this week vs 5-week avg)
    circuits_top10: list[dict]
    circuits_totals: dict
    baths_today: list[dict]
    baths_week_summary: dict
    charges_today: list[dict]
    aux_heat_kwh: float = 0.0

    @property
    def date_str(self) -> str:
        return self.target_date.strftime("%A, %B %-d")

    @property
    def daily_excl(self) -> list[tuple[date, float]]:
        return sorted(
            (d, max(0.0, k - self.daily_ev.get(d, 0.0)))
            for d, k in self.daily_grid.items()
        )

    @property
    def avg30_excl(self) -> float:
        # Drop target day from the average if it's still in progress
        series = [(d, v) for d, v in self.daily_excl if d != self.target_date] \
            if self.target_incomplete else self.daily_excl
        last = series[-30:]
        return (sum(v for _, v in last) / len(last)) if last else 0.0

    @property
    def target_incomplete(self) -> bool:
        """True when target_date is today (local), so its data is partial."""
        return self.target_date == datetime.now(LOCAL_TZ).date()

    @property
    def show_monthly(self) -> bool:
        return self.force_monthly or self.target_date.weekday() == 6

    @property
    def aux_alarm(self) -> bool:
        return self.aux_heat_kwh * ENERGY_RATE >= AUX_HEAT_ALARM_USD


def _chart_img(b64: str, alt: str) -> str:
    return (f'<img src="data:image/png;base64,{b64}" alt="{alt}" '
            f'style="width:100%;max-width:560px;display:block;margin:8px 0;">')


def _delta_arrow(current: float, baseline: float) -> str:
    if baseline == 0 or current == 0:
        return ""
    pct = (current - baseline) / baseline * 100
    arrow, color = ("&uarr;", "#e74c3c") if pct > 0 else ("&darr;", "#27ae60")
    return (f' <span style="color:{color};font-size:12px;font-weight:500;">'
            f'{arrow}{abs(pct):.0f}% vs yesterday</span>')


def _event_time(e: dict) -> str:
    t = e.get("_time")
    return t.strftime("%-I:%M %p") if hasattr(t, "strftime") else str(t)


# ---------- sections ----------

def section_aux_alarm(ctx: ReportContext) -> str:
    if not ctx.aux_alarm:
        return ""
    cost = ctx.aux_heat_kwh * ENERGY_RATE
    # 5kW resistance element ≈ 0.083 kWh/min, so kWh ÷ 0.083 ≈ minutes
    approx_min = ctx.aux_heat_kwh / (5.0 / 60.0)
    return (
        '<div style="background:#fee2e2;border-left:4px solid #dc2626;'
        'padding:12px 16px;margin:0 0 16px;border-radius:4px;color:#991b1b;">'
        f'<strong>&#9888; Auxiliary heat used</strong> &mdash; '
        f'{ctx.aux_heat_kwh:.2f} kWh (~{approx_min:.0f} min, ${cost:.2f}). '
        'See the aux-heat line on the chart below.'
        '</div>'
    )


def section_summary(ctx: ReportContext) -> str:
    delta = "" if ctx.target_incomplete else _delta_arrow(ctx.today.grid, ctx.prev_day_kwh)
    return f'''<h2>Energy Report &mdash; {ctx.date_str}</h2>

<table class="summary">
<tr><th></th><th>Today</th><th>Last 7 days</th></tr>
<tr><th>Total kWh</th><td>{ctx.today.grid:.1f}{delta}</td><td>{ctx.week.grid:.1f}</td></tr>
<tr><th>Excl. car</th><td>{ctx.today.excl:.1f}</td><td>{ctx.week.excl:.1f}</td></tr>
<tr><th>Est. cost</th><td>${ctx.today.cost:.2f}</td><td>${ctx.week.cost:.2f}</td></tr>
</table>
<p style="font-size:12px;color:#666;margin:4px 0 16px;">
30-day daily avg (excl. car): <strong>{ctx.avg30_excl:.1f} kWh/day</strong>
</p>'''


def section_today_chart(ctx: ReportContext) -> str:
    b64 = render_today_chart(ctx.today_series)
    if not b64:
        return ""
    return (f'<h3>Today &mdash; 15-min (total &amp; top categories)</h3>\n'
            f'{_chart_img(b64, "Today by 15 min")}')


def week_ev_line(ctx: ReportContext) -> str:
    """EV charging this week vs the 5- and 12-week weekly averages."""
    s = ctx.week_series
    cur, a5, a12 = ctx.week.ev, s.get("ev_avg5", 0.0), s.get("ev_avg12", 0.0)
    if cur == 0 and a5 == 0 and a12 == 0:
        return ""

    def vs(avg: float) -> str:
        if avg <= 0:
            return ""
        pct = (cur - avg) / avg * 100
        arrow, color = ("&uarr;", "#e74c3c") if pct >= 0 else ("&darr;", "#27ae60")
        return (f' <span style="color:{color};font-size:12px;font-weight:500;">'
                f'{arrow}{abs(pct):.0f}%</span>')

    return (
        f'<p style="font-size:13px;color:#444;margin:8px 0 16px;">'
        f'EV charging: <strong>{cur:.1f} kWh</strong> this week vs '
        f'<strong>{a5:.1f} kWh</strong> (5-wk avg){vs(a5)} &middot; '
        f'<strong>{a12:.1f} kWh</strong> (12-wk avg){vs(a12)}</p>'
    )


def section_week_chart(ctx: ReportContext) -> str:
    s = ctx.week_series
    b5 = render_week_compare(s, "avg5", "5-week avg (same time)", "#e67e22", ":")
    b12 = render_week_compare(s, "avg12", "12-week avg (same time)", "#16a085", "--")
    if not b5 and not b12:
        return ""
    parts = ['<h3>This week vs average (excl. car) &mdash; 2-hour grain</h3>']
    if b5:
        parts.append(_chart_img(b5, "This week vs 5-week average"))
    if b12:
        parts.append(_chart_img(b12, "This week vs 12-week average"))
    parts.append(week_ev_line(ctx))
    return "\n".join(p for p in parts if p)


def section_cost_breakdown(ctx: ReportContext) -> str:
    energy = round(ctx.today.grid * ENERGY_RATE, 2)
    base = round(BASE_CHARGE_DAILY, 2)
    return f'''<h3>Cost Breakdown &mdash; today</h3>
<table>
<tr><td>Energy &mdash; {ctx.today.grid:.2f} kWh &times; ${ENERGY_RATE:.4f}</td><td>${energy:.2f}</td></tr>
<tr><td>Base service charge</td><td>${base:.2f}</td></tr>
<tr><td><strong>Total</strong></td><td><strong>${ctx.today.cost:.2f}</strong></td></tr>
</table>
<p style="font-size:11px;color:#888;margin:4px 0;">SCL Small General, flat rate.</p>'''


def section_top_circuits(ctx: ReportContext) -> str:
    if not ctx.circuits_top10:
        return ""
    rows = "".join(
        f'<tr><td>{c["name"]}</td>'
        f'<td>{c["kwh_day"]:.2f}</td><td>${c["kwh_day"] * ENERGY_RATE:.2f}</td>'
        f'<td>{c["kwh_week"]:.2f}</td><td>${c["kwh_week"] * ENERGY_RATE:.2f}</td></tr>\n'
        for c in ctx.circuits_top10
    )
    t = ctx.circuits_totals
    totals_row = (
        f'<tr style="background:#f8f9fa;font-weight:600;">'
        f'<td>Total</td>'
        f'<td>{t["kwh_day"]:.2f}</td><td>${t["kwh_day"] * ENERGY_RATE:.2f}</td>'
        f'<td>{t["kwh_week"]:.2f}</td><td>${t["kwh_week"] * ENERGY_RATE:.2f}</td></tr>'
    )
    return f'''<h3>Usage by Category</h3>
<table>
<tr><th>Category</th><th>kWh&nbsp;(day)</th><th>$&nbsp;(day)</th><th>kWh&nbsp;(7d)</th><th>$&nbsp;(7d)</th></tr>
{rows}{totals_row}
</table>'''


def section_monthly(ctx: ReportContext) -> str:
    if not ctx.show_monthly:
        return ""
    return build_monthly_section(ctx.query_api, ctx.target_date)


def section_baths(ctx: ReportContext) -> str:
    if not (ctx.baths_today or ctx.baths_week_summary["count"] > 0):
        return ""
    today_kwh = sum((b.get("energy_kwh") or 0) for b in ctx.baths_today)
    summary = (
        f'<p style="margin:8px 0;color:#666;font-size:13px;">'
        f'Today: <strong>{len(ctx.baths_today)}</strong> '
        f'(<strong>{today_kwh:.2f} kWh</strong>, ${today_kwh * ENERGY_RATE:.2f}) &middot; '
        f'last 7 days: <strong>{ctx.baths_week_summary["count"]}</strong> '
        f'(<strong>{ctx.baths_week_summary["kwh"]:.2f} kWh</strong>, '
        f'${ctx.baths_week_summary["cost"]:.2f})</p>'
    )
    table = ""
    if ctx.baths_today:
        rows = "".join(
            f'<tr><td>{_event_time(b)}</td><td>{b.get("duration_min", 0):.0f}</td>'
            f'<td>{(b.get("energy_kwh") or 0):.2f} kWh</td>'
            f'<td>${(b.get("energy_kwh") or 0) * ENERGY_RATE:.2f}</td></tr>\n'
            for b in ctx.baths_today
        )
        table = (f'<table><tr><th>Time</th><th>Min</th><th>Energy</th><th>Cost</th></tr>\n'
                 f'{rows}</table>')
    return f'<h3>Bath Events</h3>\n{summary}\n{table}'


def section_charges(ctx: ReportContext) -> str:
    if not (ctx.charges_today or ctx.today.ev > 0 or ctx.week.ev > 0):
        return ""
    summary = (
        f'<p style="margin:8px 0;color:#666;font-size:13px;">'
        f'Today: <strong>{ctx.today.ev:.2f} kWh</strong> '
        f'(${ctx.today.ev_cost:.2f}) &middot; '
        f'last 7 days: <strong>{ctx.week.ev:.2f} kWh</strong> '
        f'(${ctx.week.ev_cost:.2f})</p>'
    )
    table = ""
    if ctx.charges_today:
        rows = "".join(
            f'<tr><td>{_event_time(ch)}</td><td>{ch.get("duration_min", 0):.0f}</td>'
            f'<td>{ch.get("mean_power_w", 0):.0f} W</td>'
            f'<td>{(ch.get("energy_kwh") or 0):.2f} kWh</td>'
            f'<td>${(ch.get("energy_kwh") or 0) * ENERGY_RATE:.2f}</td></tr>\n'
            for ch in ctx.charges_today
        )
        table = (f'<table><tr><th>Time</th><th>Min</th><th>Power</th><th>Energy</th><th>Cost</th></tr>\n'
                 f'{rows}</table>')
    return f'<h3>Car Charging</h3>\n{summary}\n{table}'


SECTIONS = [
    section_aux_alarm,
    section_summary,
    section_today_chart,
    section_week_chart,
    section_cost_breakdown,
    section_top_circuits,
    section_monthly,
    section_baths,
    section_charges,
]


CSS = """
body { font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #333; }
h2 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 8px; }
h3 { color: #2c3e50; margin-top: 24px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; }
th, td { padding: 6px 12px; text-align: left; border-bottom: 1px solid #eee; }
th { background: #f8f9fa; font-weight: 600; }
table.summary td { text-align: right; }
table.summary th:first-child, table.summary td:first-child { text-align: left; }
"""


def build_html(ctx: ReportContext) -> str:
    """Render the email body by concatenating non-empty sections."""
    body = "\n\n".join(s for s in (section(ctx) for section in SECTIONS) if s)
    return f'''<!DOCTYPE html>
<html><head><style>{CSS}</style></head>
<body>
{body}
</body></html>'''


def send_email(html: str, subject: str):
    """Send report email via Resend API."""
    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={
            "from": REPORT_FROM,
            "to": [REPORT_EMAIL],
            "subject": subject,
            "html": html,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    logger.info(f"Email sent to {REPORT_EMAIL}: {resp.json().get('id')}")


def build_monthly_section(query_api, target_date: date) -> str:
    """Render trailing-12-month chart + table. Returns HTML fragment (or '' if no data)."""
    end_y, end_m = latest_complete_month(target_date)
    start_y, start_m = add_months(end_y, end_m, -11)
    after_end_y, after_end_m = add_months(end_y, end_m, 1)

    start_dt = datetime(start_y, start_m, 1, tzinfo=LOCAL_TZ)
    end_dt = datetime(after_end_y, after_end_m, 1, tzinfo=LOCAL_TZ)
    start_str = flux_ts(start_dt.astimezone(timezone.utc))
    stop_str = flux_ts(end_dt.astimezone(timezone.utc))

    ev_pat = ev_name_filter()
    grid = dict(query_monthly_panel_kwh(query_api, start_str, stop_str))
    ev = dict(query_monthly_circuit_kwh(query_api, start_str, stop_str, ev_pat))

    months: list[tuple[int, int]] = []
    y, m = start_y, start_m
    while (y, m) != (after_end_y, after_end_m):
        months.append((y, m))
        y, m = add_months(y, m, 1)

    monthly_excl = [(ym, max(0.0, grid.get(ym, 0.0) - ev.get(ym, 0.0))) for ym in months]
    # Trim leading months with no data at all (avoids phantom base-charge rows)
    first_idx = next(
        (i for i, (ym, v) in enumerate(monthly_excl) if v > 0 or ev.get(ym, 0.0) > 0),
        len(monthly_excl),
    )
    monthly_excl = monthly_excl[first_idx:]
    if not monthly_excl:
        return ""

    chart_b64 = render_monthly_chart(monthly_excl, ev)
    chart_img = (f'<img src="data:image/png;base64,{chart_b64}" '
                 f'alt="Trailing 12 months excl. car" '
                 f'style="width:100%;max-width:560px;display:block;margin:8px 0;">') \
        if chart_b64 else ''

    rows = ""
    tot_excl = tot_ev = tot_total = tot_cost = 0.0
    for (y, m), excl in monthly_excl:
        ev_kwh = ev.get((y, m), 0.0)
        total = excl + ev_kwh
        days = calendar.monthrange(y, m)[1]
        cost = total * ENERGY_RATE + days * BASE_CHARGE_DAILY
        label = f"{calendar.month_abbr[m]} {y}"
        rows += (f'<tr><td>{label}</td><td>{excl:.1f}</td>'
                 f'<td>{ev_kwh:.1f}</td><td>{total:.1f}</td>'
                 f'<td>${cost:.2f}</td></tr>\n')
        tot_excl += excl
        tot_ev += ev_kwh
        tot_total += total
        tot_cost += cost

    total_row = (f'<tr style="background:#f8f9fa;font-weight:600;">'
                 f'<td>12-mo total</td><td>{tot_excl:.1f}</td>'
                 f'<td>{tot_ev:.1f}</td><td>{tot_total:.1f}</td>'
                 f'<td>${tot_cost:.2f}</td></tr>')

    return f'''
<h3>Trailing 12 Months</h3>
{chart_img}
<table>
<tr><th>Month</th><th>kWh excl. car</th><th>EV kWh</th><th>Total kWh</th><th>Est. cost</th></tr>
{rows}{total_row}
</table>'''


def build_context(query_api, target_date: date, force_monthly: bool) -> ReportContext:
    """Fetch everything needed for the report and pack into a ReportContext.

    Window conventions (all aligned to local midnight):
      TODAY = [target, target+1)
      WEEK  = [target-6, target+1)         — 7 days inclusive of target
      5WK   = [target-34, target+1)        — 35 days inclusive
    """
    utc_start, utc_end = local_day_utc_range(target_date)
    today_start = flux_ts(utc_start)
    today_end = flux_ts(utc_end)
    week_start = flux_ts(local_day_utc_range(target_date - timedelta(days=6))[0])
    fivewk_start = flux_ts(local_day_utc_range(target_date - timedelta(days=34))[0])
    # Week comparison averages over 12 weeks; need ≥12 complete prior weeks of history.
    weekcmp_start = flux_ts(local_day_utc_range(target_date - timedelta(days=90))[0])

    ev_pat = ev_name_filter()

    # Today + week Periods (grid total + EV total)
    today_ev_series = query_hourly_circuit_kwh(query_api, today_start, today_end, ev_pat)
    week_ev_series = query_hourly_circuit_kwh(query_api, week_start, today_end, ev_pat)
    today = Period(
        grid=query_total_kwh(query_api, today_start, today_end),
        ev=sum(v for _, v in today_ev_series),
        days=1,
    )
    week = Period(
        grid=query_total_kwh(query_api, week_start, today_end),
        ev=sum(v for _, v in week_ev_series),
        days=7,
    )

    # Previous day for "vs yesterday" delta
    pstart, pend = local_day_utc_range(target_date - timedelta(days=1))
    prev_day_kwh = query_total_kwh(query_api, flux_ts(pstart), flux_ts(pend))

    # 5-week daily series (drives 30d avg)
    daily_grid = dict(query_daily_panel_kwh(query_api, fivewk_start, today_end))
    daily_ev = dict(query_daily_circuit_kwh(query_api, fivewk_start, today_end, ev_pat))

    # Circuit breakdown
    circuits_today = query_circuit_energy(query_api, today_start, today_end)
    circuits_week = query_circuit_energy(query_api, week_start, today_end)
    top10, totals = merge_circuits(circuits_today, circuits_week, n=10)
    aux_heat_kwh = sum(
        c["kwh"] for c in circuits_today
        if AUX_CIRCUIT_PATTERN.search(c["name"])
    )

    # Line-chart series (today 15-min + this-week-vs-avg 2-hour)
    today_series = build_today_series(
        query_api, today_start, today_end, week_start,
        aux_alarm=aux_heat_kwh * ENERGY_RATE >= AUX_HEAT_ALARM_USD)
    week_series = build_week_series(query_api, weekcmp_start, today_end, target_date)

    # Events
    baths_today = query_events(query_api, "bath_event", today_start, today_end)
    baths_week = query_events(query_api, "bath_event", week_start, today_end)
    charges_today = query_events(query_api, "charge_event", today_start, today_end)

    return ReportContext(
        query_api=query_api,
        target_date=target_date,
        force_monthly=force_monthly,
        today=today,
        week=week,
        prev_day_kwh=prev_day_kwh,
        daily_grid=daily_grid,
        daily_ev=daily_ev,
        today_series=today_series,
        week_series=week_series,
        circuits_top10=top10,
        circuits_totals=totals,
        baths_today=baths_today,
        baths_week_summary=event_summary(baths_week),
        charges_today=charges_today,
        aux_heat_kwh=aux_heat_kwh,
    )


def generate_report(client: InfluxDBClient, target_date: date, force_monthly: bool = False):
    """Build the email for `target_date` (local) and send via Resend."""
    ctx = build_context(client.query_api(), target_date, force_monthly)
    prefix = "⚠ Aux heat — " if ctx.aux_alarm else ""
    subject = f"{prefix}Energy Report — {ctx.date_str}"
    send_email(build_html(ctx), subject)


def seconds_until_hour(hour: int) -> float:
    """Seconds until the next occurrence of `hour` in local time."""
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main():
    parser = argparse.ArgumentParser(description="Daily energy report email")
    parser.add_argument("--loop", action="store_true", help="Send at REPORT_HOUR daily")
    parser.add_argument("--date", type=str, help="Report for date (YYYY-MM-DD)")
    parser.add_argument("--monthly", action="store_true",
                        help="Force-include trailing-12-month section (otherwise: Sundays only)")
    args = parser.parse_args()

    for var, name in [(INFLUXDB_TOKEN, "INFLUXDB_TOKEN"), (RESEND_API_KEY, "RESEND_API_KEY"),
                      (REPORT_EMAIL, "REPORT_EMAIL")]:
        if not var:
            logger.error(f"{name} not set")
            return

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
        logger.info(f"Generating report for {args.date}")
        generate_report(client, target, force_monthly=args.monthly)
    elif args.loop:
        logger.info(f"Loop mode: report at {REPORT_HOUR}:00 daily")
        while True:
            wait = seconds_until_hour(REPORT_HOUR)
            logger.info(f"Next report in {wait / 3600:.1f} hours")
            time.sleep(wait)
            yesterday = (datetime.now() - timedelta(days=1)).date()
            try:
                generate_report(client, yesterday, force_monthly=args.monthly)
            except Exception as e:
                logger.error(f"Report failed: {e}")
    else:
        yesterday = (datetime.now() - timedelta(days=1)).date()
        generate_report(client, yesterday, force_monthly=args.monthly)

    client.close()


if __name__ == "__main__":
    main()
