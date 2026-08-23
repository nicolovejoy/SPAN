"""
    cd pi && python3 -m unittest test_collector_health -v
"""
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone

# test_weekly_report.py (run alongside this module in the combined suite)
# installs a bare stub for "httpx" via sys.modules.setdefault(), so it can
# import daily_report.py without the real dependency. That stub lacks the
# real exception classes this module needs -- drop it so we get the real
# package, which is actually installed (it's a collector dependency).
if "httpx" in sys.modules and not hasattr(sys.modules["httpx"], "TimeoutException"):
    del sys.modules["httpx"]

import httpx

import collector_health as ch


def dt(hour, minute=0, second=0, day=1):
    return datetime(2026, 8, day, hour, minute, second, tzinfo=timezone.utc)


class ClassifyErrorTest(unittest.TestCase):
    def test_timeout_exception(self):
        self.assertEqual(ch.classify_error(httpx.TimeoutException("t")), "timeout")

    def test_connect_timeout_classifies_as_timeout_not_connect(self):
        # ConnectTimeout is a TimeoutException subclass AND a ConnectError
        # subclass (both are TransportError) -- timeout must win.
        self.assertEqual(ch.classify_error(httpx.ConnectTimeout("t")), "timeout")

    def test_connect_error(self):
        self.assertEqual(ch.classify_error(httpx.ConnectError("c")), "connect")

    def test_http_status_error_4xx(self):
        request = httpx.Request("GET", "http://example.com")
        response = httpx.Response(401, request=request)
        exc = httpx.HTTPStatusError("401", request=request, response=response)
        self.assertEqual(ch.classify_error(exc), "http_4xx")

    def test_http_status_error_5xx(self):
        request = httpx.Request("GET", "http://example.com")
        response = httpx.Response(503, request=request)
        exc = httpx.HTTPStatusError("503", request=request, response=response)
        self.assertEqual(ch.classify_error(exc), "http_5xx")

    def test_json_decode_error(self):
        try:
            json.loads("not json")
        except json.JSONDecodeError as e:
            self.assertEqual(ch.classify_error(e), "decode")
            return
        self.fail("expected JSONDecodeError")

    def test_value_error(self):
        self.assertEqual(ch.classify_error(ValueError("bad")), "decode")

    def test_other(self):
        self.assertEqual(ch.classify_error(RuntimeError("boom")), "other")


class ExpectedPollsTest(unittest.TestCase):
    def test_24h_at_30s(self):
        start = dt(0)
        stop = start + timedelta(hours=24)
        self.assertEqual(ch.expected_polls(start, stop), 2880)

    def test_23h_day(self):
        start = dt(0)
        stop = start + timedelta(hours=23)
        self.assertEqual(ch.expected_polls(start, stop), 2760)


class GapStatsTest(unittest.TestCase):
    def test_empty_list_gives_zero_coverage_and_whole_window_gap(self):
        start = dt(0)
        stop = start + timedelta(hours=24)
        stats = ch.gap_stats([], start, stop)
        self.assertEqual(stats.present, 0)
        self.assertEqual(stats.expected, 2880)
        self.assertEqual(stats.coverage, 0.0)
        self.assertEqual(stats.longest_gap_s, 24 * 3600)
        self.assertEqual(stats.longest_gap_start, start)

    def test_perfect_coverage(self):
        start = dt(0)
        stop = start + timedelta(hours=24)
        timestamps = [start + timedelta(seconds=30 * i) for i in range(2880)]
        stats = ch.gap_stats(timestamps, start, stop)
        self.assertEqual(stats.present, 2880)
        self.assertEqual(stats.expected, 2880)
        self.assertEqual(stats.coverage, 1.0)
        self.assertEqual(stats.longest_gap_s, 0)
        self.assertEqual(stats.gaps_over_5m, 0)

    def test_one_two_hour_fifty_minute_hole(self):
        start = dt(0)
        stop = start + timedelta(hours=24)
        gap_start = start + timedelta(hours=6)
        hole = timedelta(hours=2, minutes=50)
        timestamps = []
        t = start
        while t < gap_start:
            timestamps.append(t)
            t += timedelta(seconds=30)
        t = gap_start + hole
        while t < stop:
            timestamps.append(t)
            t += timedelta(seconds=30)
        stats = ch.gap_stats(timestamps, start, stop)
        self.assertEqual(stats.longest_gap_s, 10200)
        self.assertEqual(stats.gaps_over_5m, 1)

    def test_defensively_sorts_unordered_timestamps(self):
        start = dt(0)
        stop = start + timedelta(hours=1)
        timestamps = [start + timedelta(seconds=30 * i) for i in range(120)]
        shuffled = list(reversed(timestamps))
        stats = ch.gap_stats(shuffled, start, stop)
        self.assertEqual(stats.present, 120)
        self.assertEqual(stats.longest_gap_s, 0)


class GapAlertNeededTest(unittest.TestCase):
    def test_all_clear(self):
        stats = ch.GapStats(
            present=2880, expected=2880, coverage=1.0,
            longest_gap_s=0, longest_gap_start=None, gaps_over_5m=0,
        )
        self.assertFalse(ch.gap_alert_needed(stats))

    def test_triggers_on_low_coverage(self):
        stats = ch.GapStats(
            present=2000, expected=2880, coverage=2000 / 2880,
            longest_gap_s=0, longest_gap_start=None, gaps_over_5m=0,
        )
        self.assertTrue(ch.gap_alert_needed(stats))

    def test_triggers_on_longest_gap(self):
        stats = ch.GapStats(
            present=2870, expected=2880, coverage=2870 / 2880,
            longest_gap_s=1800, longest_gap_start=dt(0), gaps_over_5m=1,
        )
        self.assertTrue(ch.gap_alert_needed(stats))

    def test_custom_thresholds(self):
        stats = ch.GapStats(
            present=2850, expected=2880, coverage=2850 / 2880,
            longest_gap_s=600, longest_gap_start=dt(0), gaps_over_5m=1,
        )
        self.assertFalse(ch.gap_alert_needed(stats))
        self.assertTrue(ch.gap_alert_needed(
            stats, coverage_threshold=0.995, longest_gap_threshold_s=1800,
        ))


if __name__ == "__main__":
    unittest.main()
