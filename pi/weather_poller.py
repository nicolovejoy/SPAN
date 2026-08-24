#!/usr/bin/env python3
"""Hourly outdoor temperature/humidity/cloud-cover into InfluxDB, from Open-Meteo.

Two endpoints, two purposes:
  - archive-api.open-meteo.com : ERA5 reanalysis, accurate, ~5-day processing lag,
    goes back decades. Used for historical backfill.
  - api.open-meteo.com/v1/forecast : near-real-time via `past_days`, no lag.
    Used for the recent tail during backfill and for the ongoing hourly poll.
Both return the same {"hourly": {"time": [...], "temperature_2m": [...], ...}}
shape, so one parser serves both.
"""
import argparse
import os
import time
import logging
from datetime import date, datetime, timezone, timedelta

import httpx
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

# Default: Seattle city-center. Hourly outdoor temp doesn't vary enough across
# a few miles to need rooftop-exact coordinates -- override via .env if desired.
LATITUDE = float(os.getenv("LATITUDE", "47.6062"))
LONGITUDE = float(os.getenv("LONGITUDE", "-122.3321"))

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_FIELDS = "temperature_2m,relative_humidity_2m,cloud_cover"

# ERA5 reanalysis (the archive API's data source) isn't available for the most
# recent ~5 days. Use the forecast API's past_days for anything newer than this.
ARCHIVE_LAG_DAYS = 6


def _as_float(v):
    """Open-Meteo returns humidity/cloud_cover as JSON ints; InfluxDB fixes a
    field's type at first write, so these must go in as floats to match the
    declared `float | None` interface -- otherwise a later caller that adds
    its own float() coercion gets a silent 422 field-type conflict."""
    return None if v is None else float(v)


def _parse_hourly_response(data: dict) -> list[dict]:
    """Open-Meteo's {"hourly": {"time": [...], "temperature_2m": [...], ...}}
    -> one dict per hour. Hours with no temperature reading are dropped --
    humidity/cloud_cover are nice-to-have and pass through as None."""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    humidity = hourly.get("relative_humidity_2m", [])
    cloud = hourly.get("cloud_cover", [])

    points = []
    for i, t in enumerate(times):
        temp = temps[i] if i < len(temps) else None
        if temp is None:
            continue
        points.append({
            "time": datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc),
            "temp_f": temp,
            "humidity": _as_float(humidity[i] if i < len(humidity) else None),
            "cloud_cover": _as_float(cloud[i] if i < len(cloud) else None),
        })
    return points


def fetch_archive(http_client: httpx.Client, start_date: date, end_date: date) -> list[dict]:
    """Historical hourly weather for [start_date, end_date] (inclusive), via
    the ERA5 reanalysis archive."""
    resp = http_client.get(ARCHIVE_URL, params={
        "latitude": LATITUDE, "longitude": LONGITUDE,
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "hourly": HOURLY_FIELDS, "temperature_unit": "fahrenheit", "timezone": "UTC",
    })
    resp.raise_for_status()
    return _parse_hourly_response(resp.json())


def fetch_forecast(http_client: httpx.Client, past_days: int, forecast_days: int = 1) -> list[dict]:
    """Near-real-time hourly weather covering the last `past_days` days plus
    `forecast_days` ahead. Open-Meteo's `forecast_days` counts from today, so
    0 would drop today entirely, not just the future -- callers that only
    want what already happened should request `forecast_days=1` and then
    filter the result through `_drop_future`, rather than trying to get
    Open-Meteo to stop at "now" itself."""
    resp = http_client.get(FORECAST_URL, params={
        "latitude": LATITUDE, "longitude": LONGITUDE,
        "hourly": HOURLY_FIELDS, "temperature_unit": "fahrenheit", "timezone": "UTC",
        "past_days": past_days, "forecast_days": forecast_days,
    })
    resp.raise_for_status()
    return _parse_hourly_response(resp.json())


def _today() -> date:
    """Thin wrapper so tests can freeze "today" via mock.patch.object."""
    return datetime.now(timezone.utc).date()


def _now() -> datetime:
    """Thin wrapper so tests can freeze "now" via mock.patch.object."""
    return datetime.now(timezone.utc)


def _drop_future(points: list[dict]) -> list[dict]:
    """Filter out any point whose time is after now -- Open-Meteo's
    `forecast_days` is requested as 1 (never 0, which drops today too) so
    that genuine forecast values don't get stored as if they were
    observations."""
    now = _now()
    return [p for p in points if p["time"] <= now]


def write_weather_points(write_api, points: list[dict]) -> int:
    """Write each point to the `weather` measurement. In InfluxDB 2.x, series
    identity is (measurement, tag set, field keys) -- these points carry NO
    tags, which is what makes a write at an existing (measurement, time)
    overwrite rather than duplicate. That's what makes it safe to call this
    repeatedly over an overlapping range (e.g. the archive/forecast overlap
    in `backfill`) with no existence-check needed, unlike bath/charge events.
    Adding any tag here (e.g. source= or location=) would split the overlap
    into distinct series that no longer overwrite each other, silently
    doubling every hour in the overlap seam -- don't add one without
    re-checking this."""
    if not points:
        return 0
    for pt in points:
        point = Point("weather").field("temp_f", pt["temp_f"]).time(pt["time"])
        if pt["humidity"] is not None:
            point = point.field("humidity", pt["humidity"])
        if pt["cloud_cover"] is not None:
            point = point.field("cloud_cover", pt["cloud_cover"])
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
    return len(points)


def backfill(client: InfluxDBClient, start_date: date) -> None:
    """Historical weather from start_date through today: archive API up to
    the reanalysis lag boundary, forecast API's past_days for the recent
    tail (the two ranges overlap by a couple of days on purpose -- harmless,
    since writes overwrite by timestamp)."""
    today = _today()
    archive_end = today - timedelta(days=ARCHIVE_LAG_DAYS)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    with httpx.Client(timeout=30.0) as http_client:
        if start_date <= archive_end:
            archive_points = fetch_archive(http_client, start_date, archive_end)
            n = write_weather_points(write_api, archive_points)
            logger.info(f"Archive: wrote {n} hourly points ({start_date} to {archive_end})")

        forecast_points = fetch_forecast(http_client, past_days=ARCHIVE_LAG_DAYS + 2, forecast_days=1)
        forecast_points = _drop_future(forecast_points)
        n = write_weather_points(write_api, forecast_points)
        logger.info(f"Forecast (recent tail): wrote {n} hourly points")


def normal_run(client: InfluxDBClient) -> None:
    """Check the last 2 days (covers any missed loop iteration) and write."""
    write_api = client.write_api(write_options=SYNCHRONOUS)
    with httpx.Client(timeout=30.0) as http_client:
        points = fetch_forecast(http_client, past_days=2, forecast_days=1)
    points = _drop_future(points)
    n = write_weather_points(write_api, points)
    logger.info(f"Wrote {n} hourly weather points")


def main():
    parser = argparse.ArgumentParser(description="Hourly outdoor weather into InfluxDB")
    parser.add_argument("--backfill", action="store_true", help="Backfill historical weather (no loop)")
    parser.add_argument("--start-date", type=str, default="2026-01-04",
                       help="Backfill start date YYYY-MM-DD (default: when circuit data starts)")
    parser.add_argument("--loop", action="store_true", help="Run continuously, hourly")
    parser.add_argument("--interval", type=int, default=3600, help="Seconds between polls in loop mode")
    args = parser.parse_args()

    if not INFLUXDB_TOKEN:
        logger.error("INFLUXDB_TOKEN not set")
        return

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)

    if args.backfill:
        start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        logger.info(f"Backfilling weather from {start}")
        backfill(client, start)
    elif args.loop:
        logger.info(f"Loop mode: polling every {args.interval}s")
        while True:
            try:
                normal_run(client)
            except Exception as e:
                logger.error(f"Weather poll failed: {e}")
            time.sleep(args.interval)
    else:
        normal_run(client)

    client.close()


if __name__ == "__main__":
    main()
