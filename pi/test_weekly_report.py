"""Tests for the weekly briefing + anomaly check layer added to daily_report.py.

    cd pi && python3 test_weekly_report.py

Stubs runtime deps the same way test_daily_report_rollups.py does — nothing here
touches InfluxDB.
"""
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone

for _name in ("httpx",):
    sys.modules.setdefault(_name, types.ModuleType(_name))
if "influxdb_client" not in sys.modules:
    _ic = types.ModuleType("influxdb_client")
    _ic.InfluxDBClient = object
    sys.modules["influxdb_client"] = _ic
if "matplotlib" not in sys.modules:
    _mpl = types.ModuleType("matplotlib")
    _mpl.use = lambda *a, **k: None
    sys.modules["matplotlib"] = _mpl
    sys.modules["matplotlib.pyplot"] = types.ModuleType("matplotlib.pyplot")
    sys.modules["matplotlib.dates"] = types.ModuleType("matplotlib.dates")

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import daily_report as dr   # noqa: E402

UTC = timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


class FakeRecord:
    def __init__(self, time, value, values):
        self._time, self._value, self.values = time, value, values

    def get_time(self):
        return self._time

    def get_value(self):
        return self._value


class FakeTable:
    def __init__(self, records):
        self.records = records


class FakeApi:
    """Records the Flux it is handed; returns canned tables."""

    def __init__(self, tables=None):
        self.flux = []
        self.tables = tables or []

    def query(self, flux, org=None):
        self.flux.append(flux)
        return self.tables


class CounterFluxTest(unittest.TestCase):
    def test_reads_energy_wh_counter_on_circuit_1h_only(self):
        flux = dr._counter_kwh_flux(dr.flux_ts(utc(2026, 8, 3, 7)),
                                    dr.flux_ts(utc(2026, 8, 10, 7)), "1d")
        self.assertIn('r._measurement == "circuit_1h" and r._field == "energy_wh_counter"', flux)
        self.assertNotIn('"circuit"', flux.replace('"circuit_1h"', ''))  # no raw fallback

    def test_pacific_aligned_for_day_grid(self):
        flux = dr._counter_kwh_flux(dr.flux_ts(utc(2026, 8, 3, 7)),
                                    dr.flux_ts(utc(2026, 8, 10, 7)), "1d")
        self.assertIn('timezone.location(name: "America/Los_Angeles")', flux)

    def test_stop_stamp_recentring_is_hardcoded(self):
        # 1h period: range shifted forward a full hour, then timeShift back 30 min —
        # same recentring _circuit_kwh_flux does for stamp="stop", but with no
        # runtime detection (no raw probe).
        flux = dr._counter_kwh_flux("2026-08-03T07:00:00Z", "2026-08-10T07:00:00Z", "1d")
        self.assertIn("range(start: 2026-08-03T08:00:00Z, stop: 2026-08-10T08:00:00Z)", flux)
        self.assertIn("timeShift(duration: -1800s)", flux)

    def test_name_filter_is_applied(self):
        flux = dr._counter_kwh_flux("2026-08-03T07:00:00Z", "2026-08-10T07:00:00Z", "1d",
                                    name_filter="Heat pump|Auxiliary")
        self.assertIn("r.name =~ /(?i)Heat pump|Auxiliary/", flux)


class QueryDailyCounterTest(unittest.TestCase):
    def test_rows_are_named_dated_kwh_tuples(self):
        # a stop-stamped 1d bucket at local 2026-08-04T07:00Z covers 2026-08-03 local
        tables = [FakeTable([
            FakeRecord(utc(2026, 8, 4, 7), 1.5, {"name": "Kitchen"}),
            FakeRecord(utc(2026, 8, 5, 7), 2.25, {"name": "Kitchen"}),
        ])]
        rows = dr.query_daily_circuit_counter_kwh(
            FakeApi(tables), "2026-08-03T07:00:00Z", "2026-08-05T07:00:00Z")
        self.assertEqual(rows, [
            ("Kitchen", date(2026, 8, 3), 1.5),
            ("Kitchen", date(2026, 8, 4), 2.25),
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
