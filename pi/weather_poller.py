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
            "humidity": humidity[i] if i < len(humidity) else None,
            "cloud_cover": cloud[i] if i < len(cloud) else None,
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


def fetch_forecast(http_client: httpx.Client, past_days: int, forecast_days: int = 0) -> list[dict]:
    """Near-real-time hourly weather covering the last `past_days` days plus
    `forecast_days` ahead (0 for the ongoing poll -- we only want what already
    happened)."""
    resp = http_client.get(FORECAST_URL, params={
        "latitude": LATITUDE, "longitude": LONGITUDE,
        "hourly": HOURLY_FIELDS, "temperature_unit": "fahrenheit", "timezone": "UTC",
        "past_days": past_days, "forecast_days": forecast_days,
    })
    resp.raise_for_status()
    return _parse_hourly_response(resp.json())
