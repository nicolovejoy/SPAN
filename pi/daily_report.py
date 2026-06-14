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


def flux_ts(dt: datetime) -> str:
    """Format datetime as Flux-compatible UTC timestamp."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day_utc_range(d: date) -> tuple[datetime, datetime]:
    """Convert a local date to UTC start/end datetimes."""
    start = datetime(d.year, d.month, d.day, tzinfo=LOCAL_TZ)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


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


def query_circuit_energy(query_api, start: str, stop: str) -> list[dict]:
    """Energy per circuit in kWh, summed across tag-variants, sorted descending."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> map(fn: (r) => ({{r with _value: if r._value < 0.0 then -r._value else r._value}}))
  |> integral(unit: 1h)
  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
  |> group(columns: ["name"])
  |> sum(column: "_value")
  |> group()
  |> keep(columns: ["name", "_value"])
  |> sort(columns: ["_value"], desc: true)
'''
    results = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            results.append({"name": record.values.get("name", "Unknown"), "kwh": round(record.get_value(), 2)})
    return results


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


def car_circuit_pattern() -> re.Pattern:
    """Compile the Car-category regex from categories.json (cached)."""
    rules, _ = _load_bucket_rules()
    for category, pat in rules:
        if category == "Car":
            return pat
    return re.compile(r"Tesla|Car Charger|\bEV\b", re.IGNORECASE)


def query_circuit_kwh_by_name(query_api, start: str, stop: str) -> dict[str, float]:
    """{circuit_name: kWh} over [start, stop). Reuses query_circuit_energy shape."""
    return {c["name"]: c["kwh"] for c in query_circuit_energy(query_api, start, stop)}


def query_hourly_circuit_kwh(query_api, start: str, stop: str,
                             name_filter: str) -> list[tuple[datetime, float]]:
    """Hourly kWh summed across circuits matching name_filter regex (case-insensitive)."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> filter(fn: (r) => r.name =~ /(?i){name_filter}/)
  |> map(fn: (r) => ({{r with _value: if r._value < 0.0 then -r._value else r._value}}))
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
  |> group(columns: ["_time"])
  |> sum()
'''
    out = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            out.append((record.get_time(), record.get_value() or 0.0))
    return out


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
    nf = f'  |> filter(fn: (r) => r.name =~ /(?i){name_filter}/)\n' if name_filter else ''
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
{nf}  |> map(fn: (r) => ({{r with _value: if r._value < 0.0 then -r._value else r._value}}))
  |> aggregateWindow(
       every: {every},
       fn: (column, tables=<-) => tables |> integral(unit: 1h, column: column),
       createEmpty: false)
  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
'''
    out: list[tuple[str, datetime, float]] = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            out.append((record.values.get("name", "Unknown"),
                        record.get_time(), record.get_value() or 0.0))
    return out


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
    flux = f'''
import "timezone"
option location = timezone.location(name: "{LOCAL_TZ_NAME}")

from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> filter(fn: (r) => r.name =~ /(?i){name_filter}/)
  |> map(fn: (r) => ({{r with _value: if r._value < 0.0 then -r._value else r._value}}))
  |> aggregateWindow(
       every: 1d,
       fn: (column, tables=<-) => tables |> integral(unit: 1h, column: column),
       createEmpty: false)
  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
  |> group(columns: ["_time"])
  |> sum()
'''
    out: list[tuple[date, float]] = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            t = record.get_time().astimezone(LOCAL_TZ)
            day = (t - timedelta(seconds=1)).date()
            out.append((day, record.get_value() or 0.0))
    return out


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
    flux = f'''
import "timezone"
option location = timezone.location(name: "{LOCAL_TZ_NAME}")

from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> filter(fn: (r) => r.name =~ /(?i){name_filter}/)
  |> map(fn: (r) => ({{r with _value: if r._value < 0.0 then -r._value else r._value}}))
  |> aggregateWindow(
       every: 1mo,
       fn: (column, tables=<-) => tables |> integral(unit: 1h, column: column),
       createEmpty: false)
  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
  |> group(columns: ["_time"])
  |> sum()
'''
    out: list[tuple[tuple[int, int], float]] = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            t = record.get_time().astimezone(LOCAL_TZ)
            d = (t - timedelta(seconds=1)).date()
            out.append(((d.year, d.month), record.get_value() or 0.0))
    return out


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


def build_week_series(query_api, fivewk_start: str, today_end: str,
                      target_date: date, every: str = "2h") -> dict:
    """Last-7-days total at 2h grain vs the 5-week average for the same weekday+slot."""
    every_td = timedelta(hours=2)
    panel = query_interval_panel_kwh(query_api, fivewk_start, today_end, every)
    if not panel:
        return {"times": []}

    key_vals: dict[tuple[int, int], list[float]] = {}
    for t, v in panel:
        st = t.astimezone(LOCAL_TZ) - every_td
        key_vals.setdefault((st.weekday(), st.hour // 2), []).append(v)
    key_avg = {k: sum(xs) / len(xs) for k, xs in key_vals.items()}

    cutoff = local_day_utc_range(target_date - timedelta(days=6))[0].astimezone(LOCAL_TZ)
    times, actual, rolling = [], [], []
    for t, v in panel:
        st = t.astimezone(LOCAL_TZ) - every_td
        if st < cutoff:
            continue
        times.append(st)
        actual.append(v)
        rolling.append(key_avg.get((st.weekday(), st.hour // 2), 0.0))
    return {"times": times, "actual": actual, "rolling_avg": rolling}


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


def render_week_chart(series: dict) -> str:
    """This week's total vs 5-week same-time average, 2-hour grain."""
    times = series.get("times")
    if not times:
        return ""
    fig, ax = plt.subplots(figsize=(7, 3.4), dpi=120)
    ax.plot(times, series["rolling_avg"], color="#e67e22", linewidth=1.4,
            linestyle=":", label="5-week avg (same time)")
    ax.plot(times, series["actual"], color="#3498db", linewidth=1.8, label="This week")
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


def section_week_chart(ctx: ReportContext) -> str:
    b64 = render_week_chart(ctx.week_series)
    if not b64:
        return ""
    return (f'<h3>This week vs 5-week average &mdash; 2-hour grain</h3>\n'
            f'{_chart_img(b64, "This week vs average")}')


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

    ev_pat = car_circuit_pattern().pattern
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

    ev_pat = car_circuit_pattern().pattern

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
    week_series = build_week_series(query_api, fivewk_start, today_end, target_date)

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
