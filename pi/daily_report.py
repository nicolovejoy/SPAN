#!/usr/bin/env python3
"""Daily energy report — queries InfluxDB, sends HTML email via Resend."""

import argparse
import base64
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
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "America/Los_Angeles"))


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


def compute_cost(kwh: float) -> tuple[float, float, float]:
    """Returns (total_cost, energy_cost, base_charge) for one local day."""
    energy = kwh * ENERGY_RATE
    return round(energy + BASE_CHARGE_DAILY, 2), round(energy, 2), round(BASE_CHARGE_DAILY, 2)


def query_circuit_energy(query_api, start: str, stop: str) -> list[dict]:
    """Energy per circuit in kWh, sorted descending."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> map(fn: (r) => ({{r with _value: if r._value < 0.0 then -r._value else r._value}}))
  |> integral(unit: 1h)
  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
  |> keep(columns: ["name", "_value"])
  |> group()
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


def _localize(series: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    return [(t.astimezone(LOCAL_TZ), v) for t, v in series]


def _subtract_series(a: list[tuple[datetime, float]],
                     b: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """a - b, aligned by timestamp; missing b values treated as 0."""
    bmap = {t: v for t, v in b}
    return [(t, max(0.0, v - bmap.get(t, 0.0))) for t, v in a]


def _daily_totals(series: list[tuple[datetime, float]]) -> list[tuple[date, float]]:
    """Sum hourly series into per-local-date totals, sorted."""
    by_day: dict[date, float] = {}
    for t, v in series:
        by_day[t.date()] = by_day.get(t.date(), 0.0) + v
    return sorted(by_day.items())


def render_today_chart(today_hourly: list[tuple[datetime, float]]) -> str:
    """PNG (base64) of today hourly bars."""
    today_local = _localize(today_hourly)
    if not today_local:
        return ""

    hours = [t.hour for t, _ in today_local]
    values = [v for _, v in today_local]

    fig, ax = plt.subplots(figsize=(7, 3), dpi=120)
    ax.bar(hours, values, width=0.8, color="#3498db")
    ax.set_xlabel("Hour of day (local)")
    ax.set_ylabel("kWh")
    ax.set_xticks(range(0, 24, 3))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return _fig_to_b64(fig)


def render_week_chart(daily: list[tuple[date, float]],
                      avg30: float) -> str:
    """PNG (base64) of last-7-days bars + 30-day-avg overlay."""
    if not daily:
        return ""
    days = [d for d, _ in daily]
    values = [v for _, v in daily]
    labels = [d.strftime("%a %-m/%-d") for d in days]

    fig, ax = plt.subplots(figsize=(7, 3), dpi=120)
    ax.bar(range(len(days)), values, width=0.7, color="#3498db", label="Daily")
    ax.axhline(avg30, color="#e67e22", linewidth=2, linestyle="--",
               label=f"30-day avg ({avg30:.1f} kWh)")
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel("kWh")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    return _fig_to_b64(fig)


def render_weekday_chart(daily: list[tuple[date, float]]) -> str:
    """PNG (base64): avg kWh per weekday (Mon..Sun) over the last 5 weeks.

    `daily` is the trailing 35 calendar days (excl. car) ending the day before target.
    """
    if not daily:
        return ""
    by_dow: dict[int, list[float]] = {}
    for d, v in daily:
        by_dow.setdefault(d.weekday(), []).append(v)
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    means = [(sum(by_dow.get(i, [])) / len(by_dow[i])) if by_dow.get(i) else 0.0
             for i in range(7)]

    fig, ax = plt.subplots(figsize=(7, 3), dpi=120)
    ax.bar(range(7), means, width=0.7, color="#16a085")
    ax.set_xticks(range(7))
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel("Avg kWh")
    ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout()
    return _fig_to_b64(fig)


def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


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


def build_html(date_str, kwh, cost_total, cost_energy, cost_base,
               prev_kwh, avg30_kwh, circuits, baths, charges,
               kwh_excl_car, car_kwh_today, car_kwh_week,
               today_chart_b64, week_chart_b64, weekday_chart_b64):
    """Build HTML email body."""

    def delta(current, baseline):
        if baseline == 0:
            return ""
        pct = (current - baseline) / baseline * 100
        arrow, color = ("&uarr;", "#e74c3c") if pct > 0 else ("&darr;", "#27ae60")
        return f' <span style="color:{color}">{arrow}{abs(pct):.0f}%</span>'

    circuit_rows = ""
    for c in circuits[:10]:
        est = round(c["kwh"] * ENERGY_RATE, 2)
        circuit_rows += f'<tr><td>{c["name"]}</td><td>{c["kwh"]:.2f}</td><td>${est:.2f}</td></tr>\n'

    bath_rows = ""
    for b in baths:
        t = b.get("_time")
        ts = t.strftime("%-I:%M %p") if hasattr(t, "strftime") else str(t)
        bath_rows += (f'<tr><td>{ts}</td><td>{b.get("duration_min", 0):.0f}</td>'
                      f'<td>{b.get("energy_kwh", 0):.2f} kWh</td>'
                      f'<td>${b.get("cost_dollars", 0):.2f}</td></tr>\n')

    charge_rows = ""
    for ch in charges:
        t = ch.get("_time")
        ts = t.strftime("%-I:%M %p") if hasattr(t, "strftime") else str(t)
        charge_rows += (f'<tr><td>{ts}</td><td>{ch.get("duration_min", 0):.0f}</td>'
                        f'<td>{ch.get("mean_power_w", 0):.0f} W</td>'
                        f'<td>{ch.get("energy_kwh", 0):.2f} kWh</td>'
                        f'<td>${ch.get("cost_dollars", 0):.2f}</td></tr>\n')

    bath_section = ""
    if baths:
        bath_section = f'''
<h3>Bath Events ({len(baths)})</h3>
<table><tr><th>Time</th><th>Min</th><th>Energy</th><th>Cost</th></tr>
{bath_rows}</table>'''

    charge_section = ""
    if charges or car_kwh_today > 0 or car_kwh_week > 0:
        car_summary = (f'<p style="margin:8px 0;color:#666;font-size:13px;">'
                       f'Car charging today: <strong>{car_kwh_today:.2f} kWh</strong> '
                       f'(${car_kwh_today * ENERGY_RATE:.2f}) &middot; '
                       f'last 7 days: <strong>{car_kwh_week:.2f} kWh</strong> '
                       f'(${car_kwh_week * ENERGY_RATE:.2f})</p>')
        table = (f'<table><tr><th>Time</th><th>Min</th><th>Power</th><th>Energy</th><th>Cost</th></tr>\n'
                 f'{charge_rows}</table>') if charges else ''
        charge_section = f'''
<h3>Car Charging ({len(charges)} session{"s" if len(charges) != 1 else ""})</h3>
{car_summary}
{table}'''

    today_chart_img = (f'<img src="data:image/png;base64,{today_chart_b64}" '
                       f'alt="Today hourly" style="width:100%;max-width:560px;display:block;margin:8px 0;">') \
        if today_chart_b64 else ''
    week_chart_img = (f'<img src="data:image/png;base64,{week_chart_b64}" '
                      f'alt="7-day daily" style="width:100%;max-width:560px;display:block;margin:8px 0;">') \
        if week_chart_b64 else ''
    weekday_chart_img = (f'<img src="data:image/png;base64,{weekday_chart_b64}" '
                         f'alt="Avg by weekday, 5-week" style="width:100%;max-width:560px;display:block;margin:8px 0;">') \
        if weekday_chart_b64 else ''

    return f'''<!DOCTYPE html>
<html><head><style>
body {{ font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #333; }}
h2 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 8px; }}
h3 {{ color: #2c3e50; margin-top: 24px; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0; }}
th, td {{ padding: 6px 12px; text-align: left; border-bottom: 1px solid #eee; }}
th {{ background: #f8f9fa; font-weight: 600; }}
.stats {{ display: flex; gap: 8px; margin: 16px 0; }}
.stat {{ flex: 1; text-align: center; padding: 12px; background: #f8f9fa; border-radius: 8px; }}
.stat .val {{ font-size: 24px; font-weight: 700; }}
.stat .lbl {{ font-size: 12px; color: #666; }}
</style></head>
<body>
<h2>Energy Report &mdash; {date_str}</h2>

<div class="stats">
<div class="stat"><div class="val">{kwh:.1f} kWh</div><div class="lbl">Consumption{delta(kwh, prev_kwh)}</div></div>
<div class="stat"><div class="val">${cost_total:.2f}</div><div class="lbl">Est. Cost</div></div>
<div class="stat"><div class="val">{avg30_kwh:.1f} kWh</div><div class="lbl">30-Day Avg</div></div>
</div>

<h3>Today &mdash; hourly (excl. car)</h3>
{today_chart_img}

<h3>Last 7 days &mdash; daily (excl. car)</h3>
{week_chart_img}

<h3>Avg by weekday &mdash; last 5 weeks (excl. car)</h3>
{weekday_chart_img}

<h3>Cost Breakdown</h3>
<table>
<tr><td>Energy &mdash; {kwh:.1f} kWh &times; ${ENERGY_RATE:.4f}</td><td>${cost_energy:.2f}</td></tr>
<tr><td>Base service charge</td><td>${cost_base:.2f}</td></tr>
<tr><td><strong>Total</strong></td><td><strong>${cost_total:.2f}</strong></td></tr>
</table>
<p style="font-size:11px;color:#888;margin:4px 0;">SCL Small General, flat rate.</p>

<h3>Top 10 Circuits</h3>
<table><tr><th>Circuit</th><th>kWh</th><th>Est. Cost</th></tr>
{circuit_rows}</table>
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


def generate_report(client: InfluxDBClient, target_date: date):
    """Generate and send report for a specific local date (midnight-to-midnight)."""
    query_api = client.query_api()

    utc_start, utc_end = local_day_utc_range(target_date)
    start_str = flux_ts(utc_start)
    end_str = flux_ts(utc_end)
    date_str = target_date.strftime("%A, %B %-d")

    kwh = query_total_kwh(query_api, start_str, end_str)
    cost_total, cost_energy, cost_base = compute_cost(kwh)

    # Previous day for comparison
    prev_start, prev_end = local_day_utc_range(target_date - timedelta(days=1))
    prev_kwh = query_total_kwh(query_api, flux_ts(prev_start), flux_ts(prev_end))

    # 35-day window preceding target — feeds 7d view, 30d avg, and 5-week weekday means
    LOOKBACK_DAYS = 35
    month_start, _ = local_day_utc_range(target_date - timedelta(days=LOOKBACK_DAYS))
    month_start_str = flux_ts(month_start)
    # 7-day window for car summary
    week_start, _ = local_day_utc_range(target_date - timedelta(days=7))
    week_start_str = flux_ts(week_start)

    circuits = query_circuit_energy(query_api, start_str, end_str)
    baths = query_events(query_api, "bath_event", start_str, end_str)
    charges = query_events(query_api, "charge_event", start_str, end_str)

    # Car (EV) energy — today + trailing 7 days for summary; 35d for chart-subtraction
    ev_pat = ev_circuit_pattern().pattern
    car_today_series = query_hourly_circuit_kwh(query_api, start_str, end_str, ev_pat)
    car_week_series = query_hourly_circuit_kwh(query_api, week_start_str, start_str, ev_pat)
    car_month_series = query_hourly_circuit_kwh(query_api, month_start_str, start_str, ev_pat)
    car_kwh_today = sum(v for _, v in car_today_series)
    car_kwh_week = sum(v for _, v in car_week_series)
    kwh_excl_car = max(0.0, kwh - car_kwh_today)

    # Grid hourly (excl. car) for today and the 35-day history
    today_hourly_total = query_hourly_kwh(query_api, start_str, end_str)
    month_hourly_total = query_hourly_kwh(query_api, month_start_str, start_str)
    today_hourly_excl = _subtract_series(today_hourly_total, car_today_series)
    month_hourly_excl = _subtract_series(month_hourly_total, car_month_series)
    month_daily_excl = _daily_totals(_localize(month_hourly_excl))  # up to 35 entries

    # Slice for the weekly chart and compute averages
    week_daily_excl = month_daily_excl[-7:] if len(month_daily_excl) >= 7 else month_daily_excl
    last30 = month_daily_excl[-30:] if len(month_daily_excl) >= 30 else month_daily_excl
    avg30_excl_car = (sum(v for _, v in last30) / len(last30)) if last30 else 0.0

    today_chart_b64 = render_today_chart(today_hourly_excl)
    week_chart_b64 = render_week_chart(week_daily_excl, avg30_excl_car)
    weekday_chart_b64 = render_weekday_chart(month_daily_excl)

    html = build_html(date_str, kwh, cost_total, cost_energy, cost_base,
                      prev_kwh, avg30_excl_car, circuits, baths, charges,
                      kwh_excl_car, car_kwh_today, car_kwh_week,
                      today_chart_b64, week_chart_b64, weekday_chart_b64)
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
        generate_report(client, target)
    elif args.loop:
        logger.info(f"Loop mode: report at {REPORT_HOUR}:00 daily")
        while True:
            wait = seconds_until_hour(REPORT_HOUR)
            logger.info(f"Next report in {wait / 3600:.1f} hours")
            time.sleep(wait)
            yesterday = (datetime.now() - timedelta(days=1)).date()
            try:
                generate_report(client, yesterday)
            except Exception as e:
                logger.error(f"Report failed: {e}")
    else:
        yesterday = (datetime.now() - timedelta(days=1)).date()
        generate_report(client, yesterday)

    client.close()


if __name__ == "__main__":
    main()
