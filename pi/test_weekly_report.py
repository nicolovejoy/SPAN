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


class GroupingTest(unittest.TestCase):
    ROWS = [
        ("Kitchen Lights", date(2026, 8, 3), 1.0),
        ("Kitchen Lights", date(2026, 8, 10), 2.0),   # next week
        ("Heat pump", date(2026, 8, 3), 5.0),
        ("Heat pump", date(2026, 8, 4), 6.0),
        ("Tesla Car Charger", date(2026, 8, 3), 10.0),
    ]

    def test_local_week_start_is_the_monday_on_or_before(self):
        self.assertEqual(dr.local_week_start(date(2026, 8, 3)), date(2026, 8, 3))  # Monday
        self.assertEqual(dr.local_week_start(date(2026, 8, 9)), date(2026, 8, 3))  # Sunday

    def test_category_day_kwh_rolls_up_via_display_bucket(self):
        out = dr.category_day_kwh(self.ROWS)
        self.assertEqual(out[date(2026, 8, 3)], {"Lights": 1.0, "HVAC": 5.0, "Car": 10.0})
        self.assertEqual(out[date(2026, 8, 4)], {"HVAC": 6.0})

    def test_week_totals_sums_seven_days_from_monday(self):
        day_cat = dr.category_day_kwh(self.ROWS)
        self.assertEqual(dr.week_totals(day_cat, date(2026, 8, 3)),
                         {"Lights": 1.0, "HVAC": 11.0, "Car": 10.0})
        self.assertEqual(dr.week_totals(day_cat, date(2026, 8, 10)), {"Lights": 2.0})

    def test_circuit_week_totals_stays_at_circuit_granularity(self):
        self.assertEqual(dr.circuit_week_totals(self.ROWS, date(2026, 8, 3)),
                         {"Kitchen Lights": 1.0, "Heat pump": 11.0, "Tesla Car Charger": 10.0})

    def test_category_top_circuits_filters_and_sorts_descending(self):
        rows = self.ROWS + [("Auxiliary", date(2026, 8, 3), 1.0)]
        top = dr.category_top_circuits(rows, date(2026, 8, 3), "HVAC")
        self.assertEqual(top, [("Heat pump", 11.0), ("Auxiliary", 1.0)])

    def test_trailing_week_starts_is_oldest_first_excluding_target(self):
        got = dr.trailing_week_starts(date(2026, 8, 17), 3)
        self.assertEqual(got, [date(2026, 7, 27), date(2026, 8, 3), date(2026, 8, 10)])

    def test_sum_days_is_half_open(self):
        daily = {date(2026, 8, 3): 1.0, date(2026, 8, 4): 2.0, date(2026, 8, 10): 99.0}
        self.assertEqual(dr._sum_days(daily, date(2026, 8, 3), date(2026, 8, 5)), 3.0)

    def test_unmonitored_is_panel_minus_known_circuits_floored_at_zero(self):
        self.assertEqual(dr.unmonitored_week_kwh(100.0, {"a": 40.0, "b": 30.0}), 30.0)
        self.assertEqual(dr.unmonitored_week_kwh(50.0, {"a": 60.0}), 0.0)  # never negative

    def test_all_categories_matches_categories_json_plus_default(self):
        cats = dr._all_categories()
        self.assertEqual(cats, ["Lights", "HVAC", "Car", "Appliances", "Else"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
