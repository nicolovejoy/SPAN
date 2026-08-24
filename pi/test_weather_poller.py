"""Tests for weather_poller.py — Open-Meteo ingestion into InfluxDB.

    cd pi && python3 test_weather_poller.py

Nothing here touches the network or a real InfluxDB.
"""
import sys
import types
import unittest
from datetime import date, datetime, timezone, timedelta
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
