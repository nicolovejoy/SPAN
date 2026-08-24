# Outdoor Temperature Ingestion (#14 Phase 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Written 2026-08-24 for a Sonnet-class agent; each task is self-contained and lands as its own commit.

**Goal:** Add hourly outdoor temperature (+ humidity, cloud cover) to InfluxDB as a new `weather` measurement, sourced from Open-Meteo (no API key), backfilled to 2026-01-04 (when circuit data starts). This is sub-project 1 of 2 — it exists to unblock the heat/cool split of the Stiebel Eltron heat pump (issue #14's stated reason: power signature alone can't distinguish heating from cooling) and issue #3's cold-weather aux-heat suppression. **Neither of those is in scope here** — this plan ships ingestion only.

**Architecture:** One new file, `pi/weather_poller.py`, following the exact shape of `pi/bath_detector.py` / `pi/charge_detector.py` (env-var config, `--backfill`/`--loop` CLI, InfluxDB write). Two Open-Meteo endpoints are needed because they cover different time ranges: the **archive API** (ERA5 reanalysis, has a ~5-day processing lag but goes back decades) for historical backfill, and the **forecast API** (`past_days` param, near-real-time) for the recent tail and the ongoing hourly poll. Both return the same JSON shape, so one parser serves both. Writes are naturally idempotent — an Influx point at the same measurement+timestamp overwrites, so unlike `bath_detector.py`'s event dedup, no existence-check is needed.

**Tech Stack:** Python 3.11, `httpx` (already a dependency via `daily_report.py`/`collector.py`), `influxdb-client`, stdlib `unittest`/`unittest.mock`.

**Spec:** GitHub issue #14 ("Ingest outdoor temperature") — its Phase 1 section is the design; this plan implements it directly. No separate spec doc: issue #14 is already a complete, uncontested design (confirmed with Nico 2026-08-24), matching the precedent of `docs/superpowers/plans/2026-08-22-pi-observability.md` (#16), which also went straight from issue to plan.

## Global Constraints

- **UTC at rest.** Open-Meteo's `timezone=UTC` param returns hour labels like `"2026-08-20T00:00"` with no offset — parse as UTC explicitly (`.replace(tzinfo=timezone.utc)`), never assume local time.
- **No secrets in the session.** `pi/.env` is off-limits to read directly. `INFLUXDB_TOKEN` etc. reach the container via `env_file: .env` in compose, same as every other service.
- **Pi access.** `ssh nico@phrpi.local`. Deployed code lives at `/home/nico/SPAN`. Deploy with `cd /home/nico/SPAN/pi && git pull --ff-only && docker compose build weather && docker compose up -d weather`. Do **not** rebuild `influxdb` or `grafana`. Do not run `docker compose down`.
- **New `pi/*.py` file needs a `Dockerfile` `COPY` line** — easy to forget (bit the Pi deploy before; see project memory `pi-deploy-checkout-and-dockerfile.md`).
- **Tests:** `cd pi && python3 test_weather_poller.py -v` must pass before every commit. Also re-run `python3 test_weekly_report.py` (unaffected, but cheap insurance) before the final commit.
- **No API key required** for Open-Meteo's free tier — don't add credential plumbing for it.

## File Structure

- `pi/weather_poller.py` — **new**. Fetch (archive + forecast), parse, write, backfill/normal-run orchestration, CLI.
- `pi/test_weather_poller.py` — **new**. Unit tests; httpx and `influxdb_client` calls are mocked, no real network or Influx access.
- `pi/Dockerfile` — **modify**: add `COPY weather_poller.py .`.
- `pi/docker-compose.yml` — **modify**: new `weather` service, same shape as `bath-detector`.
- `pi/.env.example` — **modify**: document optional `LATITUDE`/`LONGITUDE` overrides.
- `CLAUDE.md` — **modify**: architecture list (new service), Next Steps (note #14 Phase 1 done, split is sub-project 2).

---

### Task 1: Fetch + parse Open-Meteo responses

**Files:**
- Create: `pi/weather_poller.py`
- Test: `pi/test_weather_poller.py`

**Interfaces:**
- Produces: `_parse_hourly_response(data: dict) -> list[dict]` — each dict is `{"time": datetime (UTC), "temp_f": float, "humidity": float | None, "cloud_cover": float | None}`.
- Produces: `fetch_archive(http_client: httpx.Client, start_date: date, end_date: date) -> list[dict]`
- Produces: `fetch_forecast(http_client: httpx.Client, past_days: int, forecast_days: int = 0) -> list[dict]`
- Produces: module constants `LATITUDE`, `LONGITUDE` (floats, env-overridable, default Seattle `47.6062` / `-122.3321`)

- [ ] **Step 1: Write the failing tests**

```python
# pi/test_weather_poller.py
"""Tests for weather_poller.py — Open-Meteo ingestion into InfluxDB.

    cd pi && python3 test_weather_poller.py

Nothing here touches the network or a real InfluxDB.
"""
import sys
import types
import unittest
from datetime import date, datetime, timezone
from unittest import mock

if "influxdb_client" not in sys.modules:
    _ic = types.ModuleType("influxdb_client")
    _ic.InfluxDBClient = object
    # A lambda factory, not the MagicMock class itself: `Point("weather")` must
    # return a fresh, unrestricted mock. Assigning the class directly would make
    # the call `Point("weather")` bind "weather" to MagicMock's `spec` kwarg,
    # which then rejects the `.field(...)` chain weather_poller.py relies on
    # (a plain string has no `.field` attribute for the spec to allow).
    _ic.Point = lambda *a, **k: mock.MagicMock()
    sys.modules["influxdb_client"] = _ic
    _wa = types.ModuleType("influxdb_client.client.write_api")
    _wa.SYNCHRONOUS = "sync"
    sys.modules["influxdb_client.client.write_api"] = _wa

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import weather_poller as wp   # noqa: E402

UTC = timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


SAMPLE_RESPONSE = {
    "hourly": {
        "time": ["2026-08-20T00:00", "2026-08-20T01:00", "2026-08-20T02:00"],
        "temperature_2m": [58.1, 57.4, None],
        "relative_humidity_2m": [82, 85, 88],
        "cloud_cover": [40, None, 60],
    }
}


class ParseHourlyResponseTest(unittest.TestCase):
    def test_parses_each_hour_into_a_point_dict(self):
        points = wp._parse_hourly_response(SAMPLE_RESPONSE)
        self.assertEqual(points[0],
                         {"time": utc(2026, 8, 20, 0), "temp_f": 58.1, "humidity": 82, "cloud_cover": 40})
        self.assertEqual(points[1]["time"], utc(2026, 8, 20, 1))
        self.assertIsNone(points[1]["cloud_cover"])

    def test_skips_hours_with_no_temperature(self):
        # index 2 has temperature_2m: None -- unusable, must be dropped entirely
        points = wp._parse_hourly_response(SAMPLE_RESPONSE)
        self.assertEqual(len(points), 2)
        self.assertNotIn(utc(2026, 8, 20, 2), [p["time"] for p in points])

    def test_empty_hourly_block_gives_empty_list(self):
        self.assertEqual(wp._parse_hourly_response({"hourly": {"time": []}}), [])


class FetchArchiveTest(unittest.TestCase):
    def test_requests_the_archive_endpoint_with_date_range_and_fahrenheit(self):
        fake_client = mock.MagicMock()
        fake_client.get.return_value = mock.MagicMock(
            json=lambda: SAMPLE_RESPONSE, raise_for_status=lambda: None)

        wp.fetch_archive(fake_client, date(2026, 1, 4), date(2026, 8, 1))

        call_args, kwargs = fake_client.get.call_args
        self.assertEqual(call_args[0], "https://archive-api.open-meteo.com/v1/archive")
        params = kwargs["params"]
        self.assertEqual(params["start_date"], "2026-01-04")
        self.assertEqual(params["end_date"], "2026-08-01")
        self.assertEqual(params["temperature_unit"], "fahrenheit")
        self.assertEqual(params["timezone"], "UTC")
        self.assertEqual(params["latitude"], wp.LATITUDE)
        self.assertEqual(params["longitude"], wp.LONGITUDE)

    def test_returns_parsed_points(self):
        fake_client = mock.MagicMock()
        fake_client.get.return_value = mock.MagicMock(
            json=lambda: SAMPLE_RESPONSE, raise_for_status=lambda: None)
        points = wp.fetch_archive(fake_client, date(2026, 1, 4), date(2026, 8, 1))
        self.assertEqual(len(points), 2)


class FetchForecastTest(unittest.TestCase):
    def test_requests_the_forecast_endpoint_with_past_days(self):
        fake_client = mock.MagicMock()
        fake_client.get.return_value = mock.MagicMock(
            json=lambda: SAMPLE_RESPONSE, raise_for_status=lambda: None)

        wp.fetch_forecast(fake_client, past_days=2, forecast_days=0)

        call_args, kwargs = fake_client.get.call_args
        self.assertEqual(call_args[0], "https://api.open-meteo.com/v1/forecast")
        params = kwargs["params"]
        self.assertEqual(params["past_days"], 2)
        self.assertEqual(params["forecast_days"], 0)
        self.assertEqual(params["temperature_unit"], "fahrenheit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd pi && python3 test_weather_poller.py -v`
Expected: `ModuleNotFoundError: No module named 'weather_poller'` (or import error) — the module doesn't exist yet.

- [ ] **Step 3: Write the implementation**

```python
# pi/weather_poller.py
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd pi && python3 test_weather_poller.py -v`
Expected: all `ParseHourlyResponseTest`, `FetchArchiveTest`, `FetchForecastTest` cases PASS.

- [ ] **Step 5: Commit**

```bash
git add pi/weather_poller.py pi/test_weather_poller.py
git commit -m "weather_poller: fetch + parse Open-Meteo hourly data (#14)"
```

---

### Task 2: Write to InfluxDB, backfill/normal-run orchestration, CLI

**Files:**
- Modify: `pi/weather_poller.py`
- Test: `pi/test_weather_poller.py`

**Interfaces:**
- Consumes: `_parse_hourly_response`, `fetch_archive`, `fetch_forecast`, `LATITUDE`/`LONGITUDE`, `ARCHIVE_LAG_DAYS` from Task 1.
- Produces: `write_weather_points(write_api, points: list[dict]) -> int` (returns count written)
- Produces: `backfill(client: InfluxDBClient, start_date: date) -> None`
- Produces: `normal_run(client: InfluxDBClient) -> None`
- Produces: `main()` — CLI entrypoint, same flag shape as `bath_detector.py`

- [ ] **Step 1: Write the failing tests**

Append to `pi/test_weather_poller.py`:

```python
class WriteWeatherPointsTest(unittest.TestCase):
    def test_writes_one_point_per_hour_with_all_fields(self):
        write_api = mock.MagicMock()
        points = [{"time": utc(2026, 8, 20, 0), "temp_f": 58.1, "humidity": 82.0, "cloud_cover": 40.0}]

        count = wp.write_weather_points(write_api, points)

        self.assertEqual(count, 1)
        write_api.write.assert_called_once()
        _, kwargs = write_api.write.call_args
        self.assertEqual(kwargs["bucket"], wp.INFLUXDB_BUCKET)

    def test_omits_none_fields_but_still_writes(self):
        write_api = mock.MagicMock()
        points = [{"time": utc(2026, 8, 20, 0), "temp_f": 58.1, "humidity": None, "cloud_cover": None}]
        count = wp.write_weather_points(write_api, points)
        self.assertEqual(count, 1)

    def test_empty_points_writes_nothing(self):
        write_api = mock.MagicMock()
        self.assertEqual(wp.write_weather_points(write_api, []), 0)
        write_api.write.assert_not_called()


class BackfillTest(unittest.TestCase):
    def test_splits_between_archive_and_forecast_at_the_lag_boundary(self):
        # "today" is controlled via freezing wp._today for determinism
        with mock.patch.object(wp, "_today", return_value=date(2026, 8, 24)), \
             mock.patch.object(wp, "fetch_archive", return_value=[]) as archive, \
             mock.patch.object(wp, "fetch_forecast", return_value=[]) as forecast, \
             mock.patch.object(wp, "write_weather_points", return_value=0):
            client = mock.MagicMock()
            wp.backfill(client, date(2026, 1, 4))

        archive_end = date(2026, 8, 24) - timedelta(days=wp.ARCHIVE_LAG_DAYS)
        archive.assert_called_once_with(mock.ANY, date(2026, 1, 4), archive_end)
        forecast.assert_called_once_with(mock.ANY, past_days=wp.ARCHIVE_LAG_DAYS + 2, forecast_days=0)

    def test_writes_both_archive_and_forecast_points(self):
        with mock.patch.object(wp, "_today", return_value=date(2026, 8, 24)), \
             mock.patch.object(wp, "fetch_archive", return_value=[{"time": utc(2026, 1, 4, 0), "temp_f": 40.0, "humidity": None, "cloud_cover": None}]), \
             mock.patch.object(wp, "fetch_forecast", return_value=[{"time": utc(2026, 8, 23, 0), "temp_f": 60.0, "humidity": None, "cloud_cover": None}]), \
             mock.patch.object(wp, "write_weather_points") as write:
            client = mock.MagicMock()
            wp.backfill(client, date(2026, 1, 4))

        all_written = [pt for call in write.call_args_list for pt in call[0][1]]
        self.assertEqual(len(all_written), 2)


class NormalRunTest(unittest.TestCase):
    def test_polls_a_small_past_days_window_and_writes(self):
        with mock.patch.object(wp, "fetch_forecast", return_value=[
                {"time": utc(2026, 8, 24, 6), "temp_f": 65.0, "humidity": 70.0, "cloud_cover": 20.0}]) as forecast, \
             mock.patch.object(wp, "write_weather_points", return_value=1) as write:
            client = mock.MagicMock()
            wp.normal_run(client)

        forecast.assert_called_once_with(mock.ANY, past_days=2, forecast_days=0)
        write.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

(Move the existing `if __name__ == "__main__":` block from Step 1 to the end of the file — this replaces it, it doesn't duplicate it.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd pi && python3 test_weather_poller.py -v`
Expected: `AttributeError: module 'weather_poller' has no attribute 'write_weather_points'` (and similarly for `backfill`/`normal_run`/`_today`).

- [ ] **Step 3: Write the implementation**

Append to `pi/weather_poller.py` (before the old `if __name__ == "__main__":` line, which moves to the true end of the file):

```python
def _today() -> date:
    """Thin wrapper so tests can freeze "today" via mock.patch.object."""
    return datetime.now(timezone.utc).date()


def write_weather_points(write_api, points: list[dict]) -> int:
    """Write each point to the `weather` measurement. Same (measurement, time)
    overwrites in Influx, so this is safe to call repeatedly over an
    overlapping range -- no existence-check needed, unlike bath/charge events."""
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

        forecast_points = fetch_forecast(http_client, past_days=ARCHIVE_LAG_DAYS + 2, forecast_days=0)
        n = write_weather_points(write_api, forecast_points)
        logger.info(f"Forecast (recent tail): wrote {n} hourly points")


def normal_run(client: InfluxDBClient) -> None:
    """Check the last 2 days (covers any missed loop iteration) and write."""
    write_api = client.write_api(write_options=SYNCHRONOUS)
    with httpx.Client(timeout=30.0) as http_client:
        points = fetch_forecast(http_client, past_days=2, forecast_days=0)
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd pi && python3 test_weather_poller.py -v`
Expected: all tests PASS, including the new `WriteWeatherPointsTest`, `BackfillTest`, `NormalRunTest`.

- [ ] **Step 5: Commit**

```bash
git add pi/weather_poller.py pi/test_weather_poller.py
git commit -m "weather_poller: Influx write, backfill/normal-run, CLI (#14)"
```

---

### Task 3: Docker wiring, deploy, backfill, and live verification

**Files:**
- Modify: `pi/Dockerfile`
- Modify: `pi/docker-compose.yml`
- Modify: `pi/.env.example`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `weather_poller.py`'s `main()` (Task 2) as the container's entrypoint.

- [ ] **Step 1: Add the Dockerfile COPY line**

In `pi/Dockerfile`, find the block of `COPY *.py .` lines (alongside `collector.py`, `bath_detector.py`, etc.) and add:

```dockerfile
COPY weather_poller.py .
```

- [ ] **Step 2: Add the `weather` service to docker-compose.yml**

In `pi/docker-compose.yml`, add a new service, modeled directly on `bath-detector`:

```yaml
  weather:
    build: .
    container_name: weather
    restart: unless-stopped
    command: ["python", "-u", "weather_poller.py", "--loop"]
    env_file:
      - .env
    environment:
      - INFLUXDB_URL=http://influxdb:8086
      - INFLUXDB_TOKEN=${INFLUXDB_TOKEN}
      - INFLUXDB_ORG=home
      - INFLUXDB_BUCKET=span
    depends_on:
      - influxdb
```

- [ ] **Step 3: Document the optional LATITUDE/LONGITUDE override**

In `pi/.env.example`, add (commented out, since defaults are fine):

```bash
# Optional: override the default Seattle city-center coordinates used by
# weather_poller.py for hourly outdoor temperature (LATITUDE/LONGITUDE).
# Hourly temp doesn't vary enough across a few miles to need rooftop-exact
# values -- only set these for a meaningfully different location.
# LATITUDE=47.6062
# LONGITUDE=-122.3321
```

- [ ] **Step 4: Update CLAUDE.md**

In the `## Architecture` section's `pi/` bullet list, add a line alongside `bath_detector.py`/`charge_detector.py`, matching the existing one-line style:

```markdown
  - `weather_poller.py` - Hourly outdoor temp/humidity/cloud-cover from Open-Meteo into a
    `weather` measurement (#14 Phase 1). Unblocks the heat/cool split and cold-weather
    aux-heat suppression (#3) — neither built yet.
```

In `## Next Steps`, add a line noting Phase 1 is done and the split is separate:

```markdown
- **#14 Phase 1 done (2026-08-24)** — outdoor temp now flowing hourly into the `weather`
  measurement, backfilled to 2026-01-04. Heat/cool split + generalizing `bath_detector.py`
  into the Phase 4 attribution engine (showers, laundry hot water) is a separate
  not-yet-started sub-project — see `docs/roadmap.md` Phase 4.
```

- [ ] **Step 5: Commit the infra/doc changes**

```bash
git add pi/Dockerfile pi/docker-compose.yml pi/.env.example CLAUDE.md
git commit -m "weather: wire up Docker service + docs (#14)"
```

- [ ] **Step 6: Deploy to the Pi**

```bash
git push origin main
ssh nico@phrpi.local 'cd /home/nico/SPAN && git pull --ff-only'
ssh nico@phrpi.local 'cd /home/nico/SPAN/pi && docker compose build weather && docker compose up -d weather'
```

- [ ] **Step 7: Backfill historical weather**

Run backfill inside the running container (one-off, not the `--loop` process):

```bash
ssh nico@phrpi.local 'cd /home/nico/SPAN/pi && docker compose exec -T weather python3 weather_poller.py --backfill --start-date 2026-01-04'
```

Expected: log lines `Archive: wrote N hourly points (2026-01-04 to <date-6d>)` and `Forecast (recent tail): wrote N hourly points`, both N > 0.

- [ ] **Step 8: Verify data landed correctly**

```bash
ssh nico@phrpi.local 'cd /home/nico/SPAN/pi && docker compose exec -T influxdb influx query "
from(bucket: \"span\")
  |> range(start: 2026-01-04T00:00:00Z, stop: now())
  |> filter(fn: (r) => r._measurement == \"weather\" and r._field == \"temp_f\")
  |> count()
" --org home --token $(docker compose exec -T weather printenv INFLUXDB_TOKEN | tr -d "\r")'
```

Expected: a count in the low thousands (roughly `(days since 2026-01-04) * 24`, allowing for a handful of gaps). Cross-check a couple of individual August readings look like plausible Seattle temperatures (50s–80s°F) — if everything is drastically off (e.g., Celsius-looking values in the teens/20s on a summer day), the `temperature_unit=fahrenheit` param didn't take effect; investigate before moving on.

- [ ] **Step 9: Verify the live hourly loop is running**

```bash
ssh nico@phrpi.local 'docker logs weather --tail 20'
```

Expected: no `INFLUXDB_TOKEN not set` or repeated `Weather poll failed` errors. The container just started, so it won't have logged a poll yet unless `normal_run` fires within the observation window — that's fine; the absence of errors on startup is the pass condition here. If you want to confirm a live poll without waiting an hour, re-run Step 7's exec command manually — `normal_run`'s logic (via `--backfill` isn't quite it; instead run `docker compose exec -T weather python3 weather_poller.py` with no flags, which calls `normal_run` once) and confirm it logs `Wrote N hourly weather points` with N > 0.

---

## Explicitly Out of Scope

- The heat/cool/hot-water split itself (Phase 4 of the roadmap) — this plan only makes the `weather` measurement exist.
- Cold-weather aux-heat suppression (#3) — depends on this, not part of it.
- Generalizing `bath_detector.py` into a trigger+response-window attribution engine (showers, laundry hot water) — separate sub-project, needs the heat/cool split's classification logic first since it changes what "baseline" means.
- Per-facade solar-gain sensors (#14 Phase 2, hardware) — explicitly deferred in the issue itself.
