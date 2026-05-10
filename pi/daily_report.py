#!/usr/bin/env python3
"""Daily energy report — queries InfluxDB, sends HTML email via Resend."""

import argparse
import os
import time
import logging
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx
from influxdb_client import InfluxDBClient

from rates import get_rate, is_peak

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
REPORT_FROM = os.getenv("REPORT_FROM", "SPAN Monitor <energy@mail.pianohouseproject.org>")
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


def query_hourly_power(query_api, start: str, stop: str) -> list[dict]:
    """Hourly mean grid power for TOU cost calculation."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "panel" and r._field == "grid_power_w")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
'''
    results = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for record in table.records:
            results.append({"time": record.get_time(), "power_w": record.get_value() or 0})
    return results


def compute_tou_cost(hourly: list[dict]) -> tuple[float, float, float]:
    """Returns (total_cost, peak_cost, off_peak_cost)."""
    peak = off_peak = 0.0
    for h in hourly:
        kwh = h["power_w"] / 1000.0
        cost = kwh * get_rate(h["time"])
        if is_peak(h["time"]):
            peak += cost
        else:
            off_peak += cost
    return round(peak + off_peak, 2), round(peak, 2), round(off_peak, 2)


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


def build_html(date_str, kwh, cost_total, cost_peak, cost_off_peak,
               prev_kwh, avg7_kwh, circuits, baths, charges, avg_rate):
    """Build HTML email body."""

    def delta(current, baseline):
        if baseline == 0:
            return ""
        pct = (current - baseline) / baseline * 100
        arrow, color = ("&uarr;", "#e74c3c") if pct > 0 else ("&darr;", "#27ae60")
        return f' <span style="color:{color}">{arrow}{abs(pct):.0f}%</span>'

    circuit_rows = ""
    for c in circuits[:10]:
        est = round(c["kwh"] * avg_rate, 2)
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
    if charges:
        charge_section = f'''
<h3>Car Charging ({len(charges)})</h3>
<table><tr><th>Time</th><th>Min</th><th>Power</th><th>Energy</th><th>Cost</th></tr>
{charge_rows}</table>'''

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
<div class="stat"><div class="val">{avg7_kwh:.1f} kWh</div><div class="lbl">7-Day Avg</div></div>
</div>

<h3>Cost Breakdown</h3>
<table>
<tr><td>Peak (4-9pm weekdays)</td><td>${cost_peak:.2f}</td></tr>
<tr><td>Off-Peak</td><td>${cost_off_peak:.2f}</td></tr>
<tr><td><strong>Total</strong></td><td><strong>${cost_total:.2f}</strong></td></tr>
</table>

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
    """Generate and send report for a specific local date."""
    query_api = client.query_api()

    utc_start, utc_end = local_day_utc_range(target_date)
    start_str = flux_ts(utc_start)
    end_str = flux_ts(utc_end)
    date_str = target_date.strftime("%A, %B %-d")

    kwh = query_total_kwh(query_api, start_str, end_str)

    hourly = query_hourly_power(query_api, start_str, end_str)
    cost_total, cost_peak, cost_off_peak = compute_tou_cost(hourly)
    avg_rate = cost_total / kwh if kwh > 0 else 0.42

    # Previous day for comparison
    prev_start, prev_end = local_day_utc_range(target_date - timedelta(days=1))
    prev_kwh = query_total_kwh(query_api, flux_ts(prev_start), flux_ts(prev_end))

    # 7-day average (7 days before target)
    avg_range_start = local_day_utc_range(target_date - timedelta(days=7))[0]
    total_7d = query_total_kwh(query_api, flux_ts(avg_range_start), start_str)
    avg7_kwh = total_7d / 7 if total_7d > 0 else 0

    circuits = query_circuit_energy(query_api, start_str, end_str)
    baths = query_events(query_api, "bath_event", start_str, end_str)
    charges = query_events(query_api, "charge_event", start_str, end_str)

    html = build_html(date_str, kwh, cost_total, cost_peak, cost_off_peak,
                      prev_kwh, avg7_kwh, circuits, baths, charges, avg_rate)
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
