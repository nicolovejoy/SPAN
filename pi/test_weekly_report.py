"""Tests for the weekly briefing + anomaly check layer added to daily_report.py.

    cd pi && python3 test_weekly_report.py

Stubs runtime deps the same way test_daily_report_rollups.py does — nothing here
touches InfluxDB.
"""
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

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

    class _FakeMplObject:
        """Generic no-op stand-in for a matplotlib Figure or Axes — every
        attribute access returns a callable that accepts any args and does
        nothing, so chart-rendering code can run without a real matplotlib
        backend. Charts themselves are manual-verify (see the plan); these
        tests only need the rendering code path not to crash."""
        def __getattr__(self, name):
            return lambda *a, **k: None

    _plt = types.ModuleType("matplotlib.pyplot")
    _plt.subplots = lambda *a, **k: (_FakeMplObject(), _FakeMplObject())
    _plt.close = lambda *a, **k: None
    sys.modules["matplotlib.pyplot"] = _plt
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


class HeadlineTest(unittest.TestCase):
    def test_pct_delta_none_when_baseline_is_zero_or_negative(self):
        self.assertIsNone(dr._pct_delta(5.0, 0.0))

    def test_pct_delta_sign_matches_direction(self):
        self.assertAlmostEqual(dr._pct_delta(110.0, 100.0), 10.0)
        self.assertAlmostEqual(dr._pct_delta(90.0, 100.0), -10.0)

    def test_top_mover_is_the_largest_absolute_category_swing_excluding_unmonitored(self):
        stats = dr.headline_stats(
            week_kwh=210.0, last_week_kwh=200.0, trailing12_avg_kwh=195.0,
            week_cat={"HVAC": 80.0, "Lights": 20.0, "Unmonitored": 50.0},
            last_week_cat={"HVAC": 60.0, "Lights": 22.0, "Unmonitored": 10.0},
        )
        # Unmonitored swung by 40, HVAC by 20 — but Unmonitored is excluded
        self.assertEqual(stats["top_mover"], "HVAC")
        self.assertAlmostEqual(stats["top_mover_delta_kwh"], 20.0)
        self.assertAlmostEqual(stats["delta_vs_last_week_pct"], 5.0)
        self.assertAlmostEqual(stats["kwh"], 210.0)

    def test_no_movers_when_categories_are_empty(self):
        stats = dr.headline_stats(0.0, 0.0, 0.0, {}, {})
        self.assertIsNone(stats["top_mover"])


class BuildWeeklyContextTest(unittest.TestCase):
    def test_wires_queries_into_a_consistent_context(self):
        rows = [
            ("Heat pump", date(2026, 8, 3), 10.0),
            ("Heat pump", date(2026, 7, 27), 8.0),   # last week
            ("Kitchen Lights", date(2026, 8, 3), 2.0),
        ]
        panel_daily = {date(2026, 8, 3): 15.0, date(2026, 7, 27): 12.0}
        with mock.patch.object(dr, "query_daily_circuit_counter_kwh", return_value=rows), \
             mock.patch.object(dr, "query_daily_panel_kwh", return_value=list(panel_daily.items())):
            ctx = dr.build_weekly_context(object(), date(2026, 8, 3))

        self.assertEqual(ctx.week_start, date(2026, 8, 3))
        self.assertEqual(ctx.categories, ["Lights", "HVAC", "Car", "Appliances", "Else"])
        hvac_row = next(r for r in ctx.usage_rows if r["category"] == "HVAC")
        self.assertAlmostEqual(hvac_row["kwh"], 10.0)
        self.assertAlmostEqual(hvac_row["delta_week_pct"], 25.0)  # 10 vs 8
        unmon_row = next(r for r in ctx.usage_rows if r["category"] == "Unmonitored")
        self.assertAlmostEqual(unmon_row["kwh"], 3.0)  # 15 - (10 + 2)
        self.assertEqual(len(ctx.week_by_day), 7)
        self.assertEqual(ctx.week_by_day[0][0], date(2026, 8, 3))
        self.assertAlmostEqual(ctx.headline["kwh"], 15.0)


class RenderTest(unittest.TestCase):
    def make_ctx(self, **overrides):
        base = dict(
            week_start=date(2026, 8, 3), rows=[], panel_daily={}, day_cat={},
            categories=["Lights", "HVAC", "Car", "Appliances", "Else"],
            headline={"kwh": 210.0, "cost": 27.87, "delta_vs_last_week_pct": 5.0,
                     "delta_vs_12wk_pct": 7.7, "top_mover": "HVAC", "top_mover_delta_kwh": 20.0},
            usage_rows=[{"category": "HVAC", "kwh": 80.0, "cost": 9.93,
                        "delta_week_pct": 33.3, "delta_12wk_pct": 10.0,
                        "top_circuits": [("Heat pump", 75.0), ("Auxiliary", 5.0)]}],
            week_by_day=[(date(2026, 8, 3) + timedelta(days=i), {"HVAC": float(i)})
                        for i in range(7)],
            trend=[(date(2026, 8, 3) - timedelta(weeks=w), {"HVAC": float(w)})
                  for w in range(12, -1, -1)],
        )
        base.update(overrides)
        return dr.WeeklyContext(**base)

    def test_render_headline_names_the_top_mover(self):
        html = dr.render_headline(self.make_ctx())
        self.assertIn("210.0", html.replace(",", ""))
        self.assertIn("HVAC", html)

    def test_render_usage_table_includes_nested_top_circuits(self):
        html = dr.render_usage_table(self.make_ctx())
        self.assertIn("HVAC", html)
        self.assertIn("Heat pump", html)
        self.assertIn("Auxiliary", html)

    def test_render_usage_table_handles_no_baseline_gracefully(self):
        ctx = self.make_ctx(usage_rows=[{"category": "Car", "kwh": 0.0, "cost": 0.0,
                                        "delta_week_pct": None, "delta_12wk_pct": None,
                                        "top_circuits": []}])
        html = dr.render_usage_table(ctx)   # must not raise on None deltas
        self.assertIn("Car", html)

    def test_build_weekly_html_concatenates_sections(self):
        html = dr.build_weekly_html(self.make_ctx())
        self.assertIn("<html>", html)
        self.assertIn("HVAC", html)


class HvacBlockTest(unittest.TestCase):
    def test_mom_comparison_is_a_fair_partial_month_comparison(self):
        day_cat = {
            date(2026, 8, 1): {"HVAC": 3.0}, date(2026, 8, 10): {"HVAC": 4.0},
            date(2026, 7, 1): {"HVAC": 2.0}, date(2026, 7, 10): {"HVAC": 1.0},
            date(2026, 7, 25): {"HVAC": 100.0},   # after the day-10 cutoff — excluded
        }
        this_month, last_month = dr.mom_comparison(day_cat, date(2026, 8, 10), "HVAC")
        self.assertAlmostEqual(this_month, 7.0)    # Aug 1 + Aug 10
        self.assertAlmostEqual(last_month, 3.0)    # Jul 1 + Jul 10 (day-of-month cutoff)

    def test_mom_comparison_clamps_cutoff_to_shorter_month(self):
        day_cat = {
            date(2026, 2, 28): {"HVAC": 5.0},   # last valid Feb day — the clamped cutoff
            date(2026, 3, 31): {"HVAC": 9.0},   # this month, day 31
        }
        this_month, last_month = dr.mom_comparison(day_cat, date(2026, 3, 31), "HVAC")
        self.assertAlmostEqual(this_month, 9.0)
        self.assertAlmostEqual(last_month, 5.0)   # clamped to Feb 28, doesn't read into March

    def test_render_hvac_block_smoke(self):
        day_cat = {date(2026, 8, 3) + timedelta(days=i): {"HVAC": float(i)} for i in range(7)}
        day_cat.update({date(2026, 7, 27) + timedelta(days=i): {"HVAC": 1.0} for i in range(7)})
        ctx = dr.WeeklyContext(
            week_start=date(2026, 8, 3), rows=[], panel_daily={}, day_cat=day_cat,
            categories=["Lights", "HVAC", "Car", "Appliances", "Else"],
            headline={"kwh": 0, "cost": 0, "delta_vs_last_week_pct": None,
                     "delta_vs_12wk_pct": None, "top_mover": None, "top_mover_delta_kwh": 0},
            usage_rows=[], week_by_day=[(date(2026, 8, 3) + timedelta(days=i),
                                       {"HVAC": float(i)}) for i in range(7)],
            trend=[],
        )
        html = dr.render_hvac_block(ctx)
        self.assertIn("HVAC", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
