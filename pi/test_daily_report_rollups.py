#!/usr/bin/env python3
"""Unit tests for daily_report's rollup routing (#9).

    cd pi && python3 test_daily_report_rollups.py

Stubs httpx/matplotlib/influxdb_client so it runs without the report's runtime
deps — nothing here touches InfluxDB; it exercises the pure segment-planning and
Flux-shaping logic, which is where a rollup query silently goes wrong.
"""
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

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


class FakeApi:
    """Records the Flux it is handed; returns whatever `rows` says."""

    def __init__(self, rows=None):
        self.flux = []
        self.rows = rows or []

    def query(self, flux, org=None):
        self.flux.append(flux)
        return []


def use_rollups(span, stamp="stop"):
    """Pin the probe results so tests never need a server."""
    dr._ROLLUP_SPAN.clear()
    dr._ROLLUP_STAMP_AT.clear()
    dr._ROLLUP_STAMP_AT[dr.MEAS_RAW] = "stop"
    for m in (dr.MEAS_5M, dr.MEAS_1H):
        dr._ROLLUP_SPAN[m] = span
        dr._ROLLUP_STAMP_AT[m] = stamp


class GridTest(unittest.TestCase):
    def test_sub_day_grids_anchor_to_the_epoch(self):
        # verified against Influx: aggregateWindow's sub-day windows are
        # epoch-aligned, not aligned to the range start
        t = utc(2026, 7, 31, 7, 23, 41)
        self.assertEqual(dr._grid_floor(t, "2h"), utc(2026, 7, 31, 6))
        self.assertEqual(dr._grid_ceil(t, "2h"), utc(2026, 7, 31, 8))
        self.assertEqual(dr._grid_floor(t, "15m"), utc(2026, 7, 31, 7, 15))
        self.assertEqual(dr._grid_ceil(t, "15m"), utc(2026, 7, 31, 7, 30))

    def test_boundaries_are_fixed_points(self):
        t = utc(2026, 7, 31, 8)
        for every in ("15m", "1h", "2h"):
            self.assertEqual(dr._grid_floor(t, every), t)
            self.assertEqual(dr._grid_ceil(t, every), t)

    def test_day_and_month_grids_are_local(self):
        # 2026-07-31T09:00Z is 02:00 PDT
        t = utc(2026, 7, 31, 9)
        self.assertEqual(dr._grid_floor(t, "1d"), utc(2026, 7, 31, 7))
        self.assertEqual(dr._grid_ceil(t, "1d"), utc(2026, 8, 1, 7))
        self.assertEqual(dr._grid_floor(t, "1mo"), utc(2026, 7, 1, 7))
        self.assertEqual(dr._grid_ceil(t, "1mo"), utc(2026, 8, 1, 7))

    def test_day_grid_survives_dst(self):
        # PST->PDT on 2026-03-08: local midnights are 08:00Z then 07:00Z
        self.assertEqual(dr._grid_floor(utc(2026, 3, 8, 12), "1d"), utc(2026, 3, 8, 8))
        self.assertEqual(dr._grid_ceil(utc(2026, 3, 8, 12), "1d"), utc(2026, 3, 9, 7))

    def test_none_every_is_identity(self):
        t = utc(2026, 7, 31, 7, 23, 41)
        self.assertEqual(dr._grid_floor(t, None), t)
        self.assertEqual(dr._grid_ceil(t, None), t)

    def test_rollup_source_per_bucket_size(self):
        self.assertEqual(dr._rollup_src("15m"), dr.MEAS_5M)
        self.assertEqual(dr._rollup_src("1h"), dr.MEAS_1H)
        self.assertEqual(dr._rollup_src("2h"), dr.MEAS_1H)
        self.assertEqual(dr._rollup_src("1d"), dr.MEAS_1H)
        self.assertEqual(dr._rollup_src("1mo"), dr.MEAS_1H)
        self.assertEqual(dr._rollup_src(None), dr.MEAS_1H)


class SegmentTest(unittest.TestCase):
    # a 7am-for-yesterday run on 2026-07-31: windows stop at local midnight
    STOP = dr.flux_ts(utc(2026, 7, 31, 7))
    NOW = utc(2026, 7, 31, 14)          # 07:00 PDT
    SPAN = (utc(2026, 1, 4, 8), utc(2026, 7, 31, 14))

    def plan(self, start, every, span=None, stamp="stop", stop=None):
        use_rollups(span if span is not None else self.SPAN, stamp)
        return dr._circuit_segments(FakeApi(), dr.flux_ts(start),
                                    dr.flux_ts(stop) if stop else self.STOP, every)

    def assert_contiguous(self, segments, start, stop):
        self.assertEqual(segments[0][1], dr.flux_ts(start))
        self.assertEqual(segments[-1][2], dr.flux_ts(stop))
        for a, b in zip(segments, segments[1:]):
            self.assertEqual(a[2], b[1], "segments must tile the window with no gap")

    def test_no_rollups_means_one_raw_segment(self):
        use_rollups(None)
        start = utc(2026, 6, 24, 7)
        segments = dr._circuit_segments(FakeApi(), dr.flux_ts(start), self.STOP, "1d")
        self.assertEqual(segments, [(dr.MEAS_RAW, dr.flux_ts(start), self.STOP)])

    def test_kill_switch_forces_raw(self):
        original = dr.USE_ROLLUPS
        dr.USE_ROLLUPS = False
        try:
            segments = self.plan(utc(2026, 6, 24, 7), "1d")
            self.assertEqual([s[0] for s in segments], [dr.MEAS_RAW])
        finally:
            dr.USE_ROLLUPS = original

    def test_yesterday_report_reads_rollups_end_to_end(self):
        """The 7am run reports a day that closed 7h ago, so the rollup covers the
        whole window — this is the case that has to be fast. Only a sub-window
        remainder may stay on raw, where the grid doesn't divide the window (2h
        buckets are epoch-aligned, local midnight is an odd UTC hour in PDT)."""
        for start, every in [(utc(2026, 6, 24, 7), "1d"),
                             (utc(2026, 5, 2, 7), "2h"),
                             (utc(2026, 7, 30, 7), "15m"),
                             (utc(2026, 7, 24, 7), None)]:
            segments = self.plan(start, every)
            sources = [s[0] for s in segments]
            self.assertEqual(sources.count(dr._rollup_src(every)), 1, f"every={every}")
            slack = timedelta(minutes=dr._every_minutes(every) or 0)
            for src, seg_start, seg_stop in segments:
                if src == dr.MEAS_RAW:
                    span = dr._parse_flux_ts(seg_stop) - dr._parse_flux_ts(seg_start)
                    self.assertLessEqual(span, slack, f"raw {seg_start} every={every}")
            self.assert_contiguous(segments, start, utc(2026, 7, 31, 7))

    def test_window_older_than_the_backfill_gets_a_raw_head(self):
        # trailing-12-month chart: Aug 2025 through the last complete month
        start, stop = utc(2025, 8, 1, 7), utc(2026, 7, 1, 7)
        segments = self.plan(start, "1mo", stop=stop)
        self.assertEqual([s[0] for s in segments], [dr.MEAS_RAW, dr.MEAS_1H])
        # rollup starts at the first whole local month it fully covers
        self.assertEqual(segments[1][1], dr.flux_ts(utc(2026, 2, 1, 8)))
        self.assert_contiguous(segments, start, stop)

    def test_fresh_tail_stays_on_raw(self):
        """Reporting a day still in progress: the rollup task hasn't written the
        newest buckets, so the tail must come from raw rather than read as 0."""
        use_rollups((utc(2026, 1, 4, 8), utc(2026, 7, 31, 14)))
        start, stop = utc(2026, 7, 31, 7), utc(2026, 8, 1, 7)
        segments = dr._circuit_segments(FakeApi(), dr.flux_ts(start),
                                        dr.flux_ts(stop), "2h")
        # leading raw window is the grid remainder; the trailing one is the part
        # the rollup task has not caught up with yet
        self.assertEqual([s[0] for s in segments],
                         [dr.MEAS_RAW, dr.MEAS_1H, dr.MEAS_RAW])
        self.assertEqual(segments[1][2], dr.flux_ts(utc(2026, 7, 31, 14)))
        self.assert_contiguous(segments, start, stop)

    def test_segment_cuts_land_on_the_aggregation_grid(self):
        # a ragged span must still yield whole windows for the rollup segment
        span = (utc(2026, 3, 3, 5, 17), utc(2026, 7, 31, 13, 42))
        segments = self.plan(utc(2026, 1, 1, 8), "1d", span=span)
        rollup = [s for s in segments if s[0] != dr.MEAS_RAW][0]
        for bound in (rollup[1], rollup[2]):
            t = dr._parse_flux_ts(bound)
            self.assertEqual(dr._grid_floor(t, "1d"), t, f"{bound} off-grid")

    def test_no_usable_overlap_falls_back_to_raw(self):
        span = (utc(2026, 7, 31, 10), utc(2026, 7, 31, 14))   # newer than the window
        segments = self.plan(utc(2026, 6, 24, 7), "1d", span=span)
        self.assertEqual([s[0] for s in segments], [dr.MEAS_RAW])


class FluxShapeTest(unittest.TestCase):
    SPAN = (utc(2026, 1, 4, 8), utc(2026, 7, 31, 14))

    def flux(self, src, every, stamp="stop", mode="energy"):
        use_rollups(self.SPAN, stamp)
        return dr._circuit_kwh_flux(src, dr.flux_ts(utc(2026, 7, 30, 7)),
                                    dr.flux_ts(utc(2026, 7, 31, 7)), every,
                                    mode=mode, stamp=stamp)

    def test_raw_flux_is_unchanged_by_the_rollup_work(self):
        flux = self.flux(dr.MEAS_RAW, "1d")
        self.assertIn('r._measurement == "circuit" and r._field == "power_w"', flux)
        self.assertIn("integral(unit: 1h, column: column)", flux)
        self.assertNotIn("timeShift", flux)
        self.assertIn("2026-07-30T07:00:00Z", flux)       # range not shifted

    def test_rollup_sums_energy_wh_counter_instead_of_integrating(self):
        flux = dr._circuit_kwh_flux(
            "circuit_1h", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z",
            every=None, mode="energy")
        self.assertIn(
            'r._measurement == "circuit_1h" and r._field == "energy_wh_counter"',
            flux)
        self.assertNotIn('_field == "energy_wh"', flux)

    def test_rollup_lag_probe_still_calibrates_against_integral(self):
        """_rollup_stamp compares rollup sums to the RAW integral to measure tail
        lag. It must keep reading energy_wh — reading the counter would show a
        constant integral-vs-counter offset that the probe misreads as lag."""
        import inspect
        src = inspect.getsource(dr._rollup_stamp)
        self.assertIn('_field == "energy_wh"', src)
        self.assertNotIn('energy_wh_counter', src)

    def test_stop_stamped_buckets_are_re_centred(self):
        flux = self.flux(dr.MEAS_1H, "1d", stamp="stop")
        # read one period late (stamps of the buckets covering the window) …
        self.assertIn("range(start: 2026-07-30T08:00:00Z, stop: 2026-07-31T08:00:00Z)", flux)
        # … then moved to the bucket midpoint so it can't fall in the next window
        self.assertIn("timeShift(duration: -1800s)", flux)

    def test_start_stamped_buckets_are_re_centred_the_other_way(self):
        flux = self.flux(dr.MEAS_1H, "1d", stamp="start")
        self.assertIn("range(start: 2026-07-30T07:00:00Z, stop: 2026-07-31T07:00:00Z)", flux)
        self.assertIn("timeShift(duration: 1800s)", flux)

    def test_five_minute_rollup_uses_its_own_period(self):
        flux = self.flux(dr.MEAS_5M, "15m")
        self.assertIn('r._measurement == "circuit_5m"', flux)
        self.assertIn("timeShift(duration: -150s)", flux)

    def test_hourly_helper_keeps_mean_power_semantics(self):
        flux = self.flux(dr.MEAS_1H, "1h", mode="mean")
        self.assertIn('r._field == "power_w_mean"', flux)
        self.assertIn("fn: mean", flux)

    def test_local_timezone_only_for_calendar_windows(self):
        for every in ("1d", "1mo"):
            self.assertIn("timezone.location", self.flux(dr.MEAS_1H, every))
        for every in ("15m", "2h"):
            self.assertNotIn("timezone.location", self.flux(dr.MEAS_1H, every))


class DegradationTest(unittest.TestCase):
    def test_empty_rollup_segment_is_retried_against_raw(self):
        """Rollups configured but this window missing from them: a slow report
        beats one full of zeros."""
        use_rollups((utc(2026, 1, 4, 8), utc(2026, 7, 31, 14)))
        seen = []

        def run(src, start, stop):
            seen.append(src)
            return [] if src != dr.MEAS_RAW else [(utc(2026, 7, 30, 7), 1.0)]

        rows = dr._run_segments(FakeApi(), dr.flux_ts(utc(2026, 7, 30, 7)),
                                dr.flux_ts(utc(2026, 7, 31, 7)), "1d", run)
        self.assertEqual(seen, [dr.MEAS_1H, dr.MEAS_RAW])
        self.assertEqual(rows, [(utc(2026, 7, 30, 7), 1.0)])


class MergeTest(unittest.TestCase):
    def test_duplicate_keys_are_summed_not_dropped(self):
        rows = [(utc(2026, 7, 30, 7), 1.5), (utc(2026, 7, 29, 7), 2.0),
                (utc(2026, 7, 30, 7), 0.5)]
        self.assertEqual(dr._merge_keyed(rows),
                         [(utc(2026, 7, 29, 7), 2.0), (utc(2026, 7, 30, 7), 2.0)])

    def test_window_stop_stamps_map_back_to_their_local_day(self):
        self.assertEqual(dr._local_day(utc(2026, 7, 31, 7)),
                         datetime(2026, 7, 30).date())
        # PST side of the year: local midnight is 08:00Z
        self.assertEqual(dr._local_day(utc(2026, 2, 1, 8)),
                         datetime(2026, 1, 31).date())


class TimestampTest(unittest.TestCase):
    def test_flux_ts_round_trips(self):
        t = utc(2026, 7, 31, 7)
        self.assertEqual(dr._parse_flux_ts(dr.flux_ts(t)), t)

    def test_shift_ts(self):
        self.assertEqual(dr._shift_ts("2026-07-31T07:00:00Z", timedelta(hours=1)),
                         "2026-07-31T08:00:00Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
