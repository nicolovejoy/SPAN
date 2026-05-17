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
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
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


_EV_PATTERN: re.Pattern | None = None


def ev_circuit_pattern() -> re.Pattern:
    """Compile the EV-category regex from categories.json (cached)."""
    global _EV_PATTERN
    if _EV_PATTERN is not None:
        return _EV_PATTERN
    cats_path = Path(__file__).parent / "categories.json"
    pat = "Tesla|Car Charger|EV "  # fallback
    try:
        rules = json.loads(cats_path.read_text()).get("rules", [])
        for r in rules:
            if r.get("category") == "EV":
                pat = r.get("pattern", pat)
                break
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"categories.json unavailable, using default EV pattern: {e}")
    _EV_PATTERN = re.compile(pat, re.IGNORECASE)
    return _EV_PATTERN


def query_circuit_kwh_by_name(query_api, start: str, stop: str) -> dict[str, float]:
    """{circuit_name: kWh} over [start, stop). Reuses query_circuit_energy shape."""
    return {c["name"]: c["kwh"] for c in query_circuit_energy(query_api, start, stop)}


def query_hourly_kwh(query_api, start: str, stop: str) -> list[tuple[datetime, float]]:
    """Hourly grid kWh (mean power per hour, treated as kWh since window=1h)."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "panel" and r._field == "grid_power_w")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: true)
  |> map(fn: (r) => ({{r with _value: (if exists r._value then r._value else 0.0) / 1000.0}}))
'''
    out = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            out.append((record.get_time(), record.get_value() or 0.0))
    return out


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


def _localize(series: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    return [(t.astimezone(LOCAL_TZ), v) for t, v in series]


def _subtract_series(a: list[tuple[datetime, float]],
                     b: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """a - b, aligned by timestamp; missing b values treated as 0."""
    bmap = {t: v for t, v in b}
    return [(t, max(0.0, v - bmap.get(t, 0.0))) for t, v in a]


def _avg_by_hour(hourly: list[tuple[datetime, float]]) -> dict[int, float]:
    """{hour-of-day: mean kWh} from a local-time hourly series."""
    by_h: dict[int, list[float]] = {}
    for t, v in hourly:
        by_h.setdefault(t.hour, []).append(v)
    return {h: sum(vs) / len(vs) for h, vs in by_h.items() if vs}


def render_today_chart(today_hourly: list[tuple[datetime, float]],
                       week_hourly: list[tuple[datetime, float]]) -> str:
    """Today hourly bars + 7-day same-hour avg line."""
    today_local = _localize(today_hourly)
    week_local = _localize(week_hourly)
    if not today_local:
        return ""

    hours = sorted({t.hour for t, _ in today_local})
    val_by_hour = {t.hour: v for t, v in today_local}
    values = [val_by_hour.get(h, 0.0) for h in hours]

    avg_by_hour = _avg_by_hour(week_local)
    avg_line = [avg_by_hour.get(h, 0.0) for h in hours]

    fig, ax = plt.subplots(figsize=(7, 3), dpi=120)
    ax.bar(hours, values, width=0.8, color="#3498db", label="Today")
    ax.plot(hours, avg_line, color="#e67e22", linewidth=2, linestyle="--",
            label="7-day avg (same hour)")
    ax.set_xlabel("Hour of day (local)")
    ax.set_ylabel("kWh")
    ax.set_xticks(range(0, 24, 3))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    return _fig_to_b64(fig)


def render_week_daily_chart(daily_excl: list[tuple[date, float]],
                            daily_ev: dict[date, float]) -> str:
    """Grouped daily bars (stacked excl + EV): last 7 days actual vs 5-week-same-weekday avg.

    `daily_excl` is the full lookback window (e.g. 35 days), one entry per local day.
    `daily_ev` covers the same window, keyed by local date.
    """
    if not daily_excl:
        return ""

    # 5-week avg per weekday from full history — excl and EV separately
    by_wd_excl: dict[int, list[float]] = {}
    by_wd_ev: dict[int, list[float]] = {}
    for d, v in daily_excl:
        by_wd_excl.setdefault(d.weekday(), []).append(v)
        by_wd_ev.setdefault(d.weekday(), []).append(daily_ev.get(d, 0.0))
    wd_avg_excl = {wd: sum(vs) / len(vs) for wd, vs in by_wd_excl.items() if vs}
    wd_avg_ev = {wd: sum(vs) / len(vs) for wd, vs in by_wd_ev.items() if vs}

    last7 = daily_excl[-7:]
    days = [d for d, _ in last7]
    actual_excl = [v for _, v in last7]
    actual_ev = [daily_ev.get(d, 0.0) for d in days]
    avg_excl = [wd_avg_excl.get(d.weekday(), 0.0) for d in days]
    avg_ev = [wd_avg_ev.get(d.weekday(), 0.0) for d in days]
    labels = [d.strftime("%a %-m/%-d") for d in days]

    x = list(range(len(days)))
    width = 0.4
    left = [i - width / 2 for i in x]
    right = [i + width / 2 for i in x]
    fig, ax = plt.subplots(figsize=(7, 3.2), dpi=120)
    ax.bar(left, actual_excl, width, color="#3498db", label="Day excl. car")
    ax.bar(left, actual_ev, width, bottom=actual_excl, color="#9b59b6", label="Day EV")
    ax.bar(right, avg_excl, width, color="#e67e22", label="5-week avg excl.")
    ax.bar(right, avg_ev, width, bottom=avg_excl, color="#f0b27a", label="5-week avg EV")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel("kWh")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="upper left", fontsize=8, ncol=2)
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


def merge_circuits(today_list: list[dict], week_list: list[dict],
                   n: int = 10) -> tuple[list[dict], dict]:
    """Top N circuits by today's kWh, with week kWh joined in. Returns (rows, totals)."""
    week_map = {c["name"]: c["kwh"] for c in week_list}
    rows = sorted(
        ({"name": c["name"], "kwh_day": c["kwh"], "kwh_week": week_map.get(c["name"], 0.0)}
         for c in today_list),
        key=lambda r: r["kwh_day"], reverse=True,
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


def build_html(date_str, kwh_today, kwh_week, kwh_today_excl, kwh_week_excl,
               cost_today, cost_week, cost_energy_today, cost_base_today,
               prev_kwh, avg30_excl_per_day,
               circuit_rows_data, circuit_totals,
               baths_today, baths_week_summary,
               charges_today, charge_today_summary, charge_week_summary,
               today_chart_b64, week_daily_chart_b64,
               monthly_section=""):
    """Build HTML email body."""

    def delta(current, baseline):
        if baseline == 0:
            return ""
        pct = (current - baseline) / baseline * 100
        arrow, color = ("&uarr;", "#e74c3c") if pct > 0 else ("&darr;", "#27ae60")
        return (f' <span style="color:{color};font-size:12px;font-weight:500;">'
                f'{arrow}{abs(pct):.0f}% vs yesterday</span>')

    # Top 10 circuit rows + totals
    circuit_rows = ""
    for c in circuit_rows_data:
        cost_d = c["kwh_day"] * ENERGY_RATE
        cost_w = c["kwh_week"] * ENERGY_RATE
        circuit_rows += (
            f'<tr><td>{c["name"]}</td>'
            f'<td>{c["kwh_day"]:.2f}</td><td>${cost_d:.2f}</td>'
            f'<td>{c["kwh_week"]:.2f}</td><td>${cost_w:.2f}</td></tr>\n'
        )
    ct_cost_d = circuit_totals["kwh_day"] * ENERGY_RATE
    ct_cost_w = circuit_totals["kwh_week"] * ENERGY_RATE
    circuit_total_row = (
        f'<tr style="background:#f8f9fa;font-weight:600;">'
        f'<td>Total (top 10)</td>'
        f'<td>{circuit_totals["kwh_day"]:.2f}</td><td>${ct_cost_d:.2f}</td>'
        f'<td>{circuit_totals["kwh_week"]:.2f}</td><td>${ct_cost_w:.2f}</td></tr>'
    )

    # Bath section: summary line + today's events table
    bath_section = ""
    if baths_today or baths_week_summary["count"] > 0:
        bath_rows = ""
        for b in baths_today:
            t = b.get("_time")
            ts = t.strftime("%-I:%M %p") if hasattr(t, "strftime") else str(t)
            kwh = b.get("energy_kwh", 0) or 0
            bath_rows += (f'<tr><td>{ts}</td><td>{b.get("duration_min", 0):.0f}</td>'
                          f'<td>{kwh:.2f} kWh</td>'
                          f'<td>${kwh * ENERGY_RATE:.2f}</td></tr>\n')
        bath_summary_line = (
            f'<p style="margin:8px 0;color:#666;font-size:13px;">'
            f'Today: <strong>{len(baths_today)}</strong> '
            f'(<strong>{sum((b.get("energy_kwh") or 0) for b in baths_today):.2f} kWh</strong>, '
            f'${sum((b.get("energy_kwh") or 0) for b in baths_today) * ENERGY_RATE:.2f}) &middot; '
            f'last 7 days: <strong>{baths_week_summary["count"]}</strong> '
            f'(<strong>{baths_week_summary["kwh"]:.2f} kWh</strong>, '
            f'${baths_week_summary["cost"]:.2f})</p>'
        )
        bath_table = (f'<table><tr><th>Time</th><th>Min</th><th>Energy</th><th>Cost</th></tr>\n'
                      f'{bath_rows}</table>') if baths_today else ''
        bath_section = f'''
<h3>Bath Events</h3>
{bath_summary_line}
{bath_table}'''

    # Charge section: summary line + today's sessions table
    charge_section = ""
    if charges_today or charge_today_summary["kwh"] > 0 or charge_week_summary["kwh"] > 0:
        charge_rows = ""
        for ch in charges_today:
            t = ch.get("_time")
            ts = t.strftime("%-I:%M %p") if hasattr(t, "strftime") else str(t)
            kwh = ch.get("energy_kwh", 0) or 0
            charge_rows += (f'<tr><td>{ts}</td><td>{ch.get("duration_min", 0):.0f}</td>'
                            f'<td>{ch.get("mean_power_w", 0):.0f} W</td>'
                            f'<td>{kwh:.2f} kWh</td>'
                            f'<td>${kwh * ENERGY_RATE:.2f}</td></tr>\n')
        charge_summary_line = (
            f'<p style="margin:8px 0;color:#666;font-size:13px;">'
            f'Today: <strong>{charge_today_summary["kwh"]:.2f} kWh</strong> '
            f'(${charge_today_summary["cost"]:.2f}) &middot; '
            f'last 7 days: <strong>{charge_week_summary["kwh"]:.2f} kWh</strong> '
            f'(${charge_week_summary["cost"]:.2f})</p>'
        )
        charge_table = (f'<table><tr><th>Time</th><th>Min</th><th>Power</th><th>Energy</th><th>Cost</th></tr>\n'
                        f'{charge_rows}</table>') if charges_today else ''
        charge_section = f'''
<h3>Car Charging</h3>
{charge_summary_line}
{charge_table}'''

    today_chart_img = (f'<img src="data:image/png;base64,{today_chart_b64}" '
                       f'alt="Today hourly" style="width:100%;max-width:560px;display:block;margin:8px 0;">') \
        if today_chart_b64 else ''
    week_daily_chart_img = (f'<img src="data:image/png;base64,{week_daily_chart_b64}" '
                         f'alt="Last 7 days vs 5-week avg, daily" '
                         f'style="width:100%;max-width:560px;display:block;margin:8px 0;">') \
        if week_daily_chart_b64 else ''

    return f'''<!DOCTYPE html>
<html><head><style>
body {{ font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #333; }}
h2 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 8px; }}
h3 {{ color: #2c3e50; margin-top: 24px; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0; }}
th, td {{ padding: 6px 12px; text-align: left; border-bottom: 1px solid #eee; }}
th {{ background: #f8f9fa; font-weight: 600; }}
table.summary td {{ text-align: right; }}
table.summary th:first-child, table.summary td:first-child {{ text-align: left; }}
</style></head>
<body>
<h2>Energy Report &mdash; {date_str}</h2>

<table class="summary">
<tr><th></th><th>Today</th><th>Last 7 days</th></tr>
<tr><th>Total kWh</th><td>{kwh_today:.1f}{delta(kwh_today, prev_kwh)}</td><td>{kwh_week:.1f}</td></tr>
<tr><th>Excl. car</th><td>{kwh_today_excl:.1f}</td><td>{kwh_week_excl:.1f}</td></tr>
<tr><th>Est. cost</th><td>${cost_today:.2f}</td><td>${cost_week:.2f}</td></tr>
</table>
<p style="font-size:12px;color:#666;margin:4px 0 16px;">
30-day daily avg (excl. car): <strong>{avg30_excl_per_day:.1f} kWh/day</strong>
</p>

<h3>Today &mdash; hourly (excl. car)</h3>
{today_chart_img}

<h3>Last 7 days vs 5-week avg &mdash; daily (stacked: excl. car + EV)</h3>
{week_daily_chart_img}

<h3>Cost Breakdown &mdash; today</h3>
<table>
<tr><td>Energy &mdash; {kwh_today:.1f} kWh &times; ${ENERGY_RATE:.4f}</td><td>${cost_energy_today:.2f}</td></tr>
<tr><td>Base service charge</td><td>${cost_base_today:.2f}</td></tr>
<tr><td><strong>Total</strong></td><td><strong>${cost_today:.2f}</strong></td></tr>
</table>
<p style="font-size:11px;color:#888;margin:4px 0;">SCL Small General, flat rate.</p>

<h3>Top 10 Circuits</h3>
<table>
<tr><th>Circuit</th><th>kWh (day)</th><th>$ (day)</th><th>kWh (7d)</th><th>$ (7d)</th></tr>
{circuit_rows}{circuit_total_row}
</table>
{monthly_section}
{bath_section}
{charge_section}
</body></html>'''


def send_email(html: str, date_str: str):
    """Send report email via Resend API."""
    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={
            "from": REPORT_FROM,
            "to": [REPORT_EMAIL],
            "subject": f"Energy Report \u2014 {date_str}",
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

    ev_pat = ev_circuit_pattern().pattern
    grid = dict(query_monthly_panel_kwh(query_api, start_str, stop_str))
    ev = dict(query_monthly_circuit_kwh(query_api, start_str, stop_str, ev_pat))

    months: list[tuple[int, int]] = []
    y, m = start_y, start_m
    while (y, m) != (after_end_y, after_end_m):
        months.append((y, m))
        y, m = add_months(y, m, 1)

    monthly_excl = [(ym, max(0.0, grid.get(ym, 0.0) - ev.get(ym, 0.0))) for ym in months]
    if not any(v > 0 for _, v in monthly_excl):
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


def generate_report(client: InfluxDBClient, target_date: date, force_monthly: bool = False):
    """Generate and send report for a specific local date (midnight-to-midnight).

    Window conventions (all aligned to local midnight):
      TODAY = [target, target+1)
      WEEK  = [target-6, target+1)         — 7 days inclusive of target
      MONTH = [target-29, target+1)        — 30 days inclusive
      5WK   = [target-34, target+1)        — 35 days inclusive, feeds the 3h chart
    """
    query_api = client.query_api()

    utc_start, utc_end = local_day_utc_range(target_date)
    start_str = flux_ts(utc_start)
    end_str = flux_ts(utc_end)
    week_start_str = flux_ts(local_day_utc_range(target_date - timedelta(days=6))[0])
    month_start_str = flux_ts(local_day_utc_range(target_date - timedelta(days=29))[0])
    fivewk_start_str = flux_ts(local_day_utc_range(target_date - timedelta(days=34))[0])
    date_str = target_date.strftime("%A, %B %-d")

    # Totals
    kwh_today = query_total_kwh(query_api, start_str, end_str)
    kwh_week = query_total_kwh(query_api, week_start_str, end_str)

    # Previous day for "vs yesterday" delta
    prev_start, prev_end = local_day_utc_range(target_date - timedelta(days=1))
    prev_kwh = query_total_kwh(query_api, flux_ts(prev_start), flux_ts(prev_end))

    # Car (EV) — windowed totals and hourly series (for chart subtraction)
    ev_pat = ev_circuit_pattern().pattern
    car_today_series = query_hourly_circuit_kwh(query_api, start_str, end_str, ev_pat)
    car_week_series = query_hourly_circuit_kwh(query_api, week_start_str, end_str, ev_pat)
    car_kwh_today = sum(v for _, v in car_today_series)
    car_kwh_week = sum(v for _, v in car_week_series)

    kwh_today_excl = max(0.0, kwh_today - car_kwh_today)
    kwh_week_excl = max(0.0, kwh_week - car_kwh_week)

    cost_today = cost_n_days(kwh_today, 1)
    cost_week = cost_n_days(kwh_week, 7)
    cost_energy_today = round(kwh_today * ENERGY_RATE, 2)
    cost_base_today = round(BASE_CHARGE_DAILY, 2)

    # Circuit energy today AND over last 7 days, merged for the top-10 table
    circuits_today = query_circuit_energy(query_api, start_str, end_str)
    circuits_week = query_circuit_energy(query_api, week_start_str, end_str)
    circuit_rows_data, circuit_totals = merge_circuits(circuits_today, circuits_week, n=10)

    # Events: today + 7-day summaries
    baths_today = query_events(query_api, "bath_event", start_str, end_str)
    baths_week = query_events(query_api, "bath_event", week_start_str, end_str)
    charges_today = query_events(query_api, "charge_event", start_str, end_str)
    baths_week_summary = event_summary(baths_week)
    charge_today_summary = {"kwh": car_kwh_today, "cost": round(car_kwh_today * ENERGY_RATE, 2)}
    charge_week_summary = {"kwh": car_kwh_week, "cost": round(car_kwh_week * ENERGY_RATE, 2)}

    # Today chart still needs hourly series (24 points subtracted hourly)
    today_hourly_total = query_hourly_kwh(query_api, start_str, end_str)
    week_hourly_total = query_hourly_kwh(query_api, week_start_str, end_str)
    today_hourly_excl = _subtract_series(today_hourly_total, car_today_series)
    week_hourly_excl = _subtract_series(week_hourly_total, car_week_series)

    # Daily totals via per-local-day integral (matches the summary's exact-energy path).
    # One query for grid, one for EV; subtract by date.
    daily_grid = dict(query_daily_panel_kwh(query_api, fivewk_start_str, end_str))
    daily_ev = dict(query_daily_circuit_kwh(query_api, fivewk_start_str, end_str, ev_pat))
    daily_excl = sorted(
        (d, max(0.0, kwh - daily_ev.get(d, 0.0))) for d, kwh in daily_grid.items()
    )

    # 30-day daily avg (excl. car) for the helper line under the summary table
    last30 = daily_excl[-30:] if len(daily_excl) >= 30 else daily_excl
    avg30_excl_per_day = (sum(v for _, v in last30) / len(last30)) if last30 else 0.0

    today_chart_b64 = render_today_chart(today_hourly_excl, week_hourly_excl)
    week_daily_chart_b64 = render_week_daily_chart(daily_excl, daily_ev)

    monthly_section = (build_monthly_section(query_api, target_date)
                       if force_monthly or target_date.weekday() == 6 else "")

    html = build_html(
        date_str=date_str,
        kwh_today=kwh_today, kwh_week=kwh_week,
        kwh_today_excl=kwh_today_excl, kwh_week_excl=kwh_week_excl,
        cost_today=cost_today, cost_week=cost_week,
        cost_energy_today=cost_energy_today, cost_base_today=cost_base_today,
        prev_kwh=prev_kwh, avg30_excl_per_day=avg30_excl_per_day,
        circuit_rows_data=circuit_rows_data, circuit_totals=circuit_totals,
        baths_today=baths_today, baths_week_summary=baths_week_summary,
        charges_today=charges_today,
        charge_today_summary=charge_today_summary,
        charge_week_summary=charge_week_summary,
        today_chart_b64=today_chart_b64,
        week_daily_chart_b64=week_daily_chart_b64,
        monthly_section=monthly_section,
    )
    send_email(html, date_str)


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
