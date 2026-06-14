#!/usr/bin/env python3
"""One-off HVAC diagnostic: per-minute power, one line per HVAC circuit, last N hours.

Run inside the daily-report container (has matplotlib + influxdb:8086 access):

  cd pi && docker compose run --rm \
    -v "$PWD/hvac_probe.py:/app/hvac_probe.py" -v "$PWD:/out" \
    daily-report python /app/hvac_probe.py --out /out/hvac_48h.png

Reads INFLUXDB_* and TZ from the container env (same as daily_report.py).
"""

import argparse
import os
from datetime import timezone
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from influxdb_client import InfluxDBClient

INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "home")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "span")
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "America/Los_Angeles"))

# Matches the HVAC bucket in categories.json (Steibel Eltron: heat pump + control + aux).
HVAC_PATTERN = r"heat pump|auxiliary"


def query_per_circuit(query_api, hours: int, every: str) -> dict[str, list[tuple]]:
    """{circuit_name: [(local_dt, watts), ...]} for HVAC circuits, mean power per window."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> filter(fn: (r) => r.name =~ /(?i){HVAC_PATTERN}/)
  |> map(fn: (r) => ({{r with _value: if r._value < 0.0 then -r._value else r._value}}))
  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)
'''
    series: dict[str, list[tuple]] = {}
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for rec in table.records:
            name = rec.values.get("name", "Unknown")
            t = rec.get_time().astimezone(LOCAL_TZ)
            series.setdefault(name, []).append((t, rec.get_value() or 0.0))
    for s in series.values():
        s.sort(key=lambda x: x[0])
    return series


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--every", default="1m", help="bucket size (Flux duration, default 1m)")
    ap.add_argument("--out", default="hvac_probe.png")
    args = ap.parse_args()

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    series = query_per_circuit(client.query_api(), args.hours, args.every)
    client.close()

    if not series:
        print("No HVAC circuit data found.")
        return

    # Stable color/order: heaviest mean draw first.
    names = sorted(series, key=lambda n: -sum(v for _, v in series[n]) / max(1, len(series[n])))

    fig, ax = plt.subplots(figsize=(12, 5), dpi=130)
    for name in names:
        pts = series[name]
        xs = [t for t, _ in pts]
        ys = [w / 1000.0 for _, w in pts]  # kW
        peak = max(ys) if ys else 0.0
        ax.plot(xs, ys, linewidth=1.0, label=f"{name}  (peak {peak:.2f} kW)")

    ax.set_xlabel(f"Time ({LOCAL_TZ.key})")
    ax.set_ylabel("Power (kW)")
    ax.set_title(f"HVAC circuits — last {args.hours}h, per-{args.every} avg power")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M", tz=LOCAL_TZ))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(args.out, bbox_inches="tight")
    print(f"Wrote {args.out} — circuits: {', '.join(names)}")


if __name__ == "__main__":
    main()
