"""
    cd pi && python3 test_report_baseline.py
"""
import unittest
from datetime import date

import report_baseline as rb


class MedianMadTest(unittest.TestCase):
    def test_median_odd_and_even(self):
        self.assertEqual(rb.median([1, 3, 2]), 2)
        self.assertEqual(rb.median([1, 2, 3, 4]), 2.5)

    def test_mad_is_normal_consistent(self):
        # symmetric spread around 10: MAD * 1.4826 approximates stdev for a
        # normal-ish sample
        samples = [8, 9, 10, 11, 12]
        self.assertAlmostEqual(rb.mad(samples), 1.4826, places=3)

    def test_compute_baseline_reports_sample_count(self):
        b = rb.compute_baseline([1.0, 2.0, 3.0])
        self.assertEqual(b.n, 3)
        self.assertEqual(b.median, 2.0)


class EvaluateTest(unittest.TestCase):
    def test_fires_only_when_both_z_and_floor_exceeded(self):
        # 8 samples all near 10, one outlier value of 20 to evaluate
        baseline = rb.compute_baseline([9, 10, 10, 11, 10, 9, 10, 11])
        result = rb.evaluate(20.0, baseline)
        self.assertTrue(result.is_anomalous)
        self.assertGreater(abs(result.z), 3)

    def test_floor_suppresses_a_small_wobble_on_a_tiny_category(self):
        # Lights-scale baseline: median 0.3, near-zero MAD — without the floor
        # a 0.3 kWh wobble would fire on z alone
        baseline = rb.compute_baseline([0.28, 0.30, 0.29, 0.31, 0.30, 0.29, 0.30, 0.31])
        result = rb.evaluate(0.6, baseline)   # +0.3 kWh swing, well under the 1.0 kWh floor
        self.assertFalse(result.is_anomalous)

    def test_floor_scales_with_a_large_category(self):
        # Car-scale baseline: median 40 kWh — 20% floor (8 kWh) dominates the 1.0 kWh minimum
        baseline = rb.compute_baseline([38, 40, 39, 41, 40, 39, 40, 42])
        just_under = rb.evaluate(47.5, baseline)   # 7.5 kWh over — under the 8 kWh floor
        self.assertFalse(just_under.is_anomalous)

    def test_degenerate_mad_zero_falls_back_to_percentage_floor(self):
        baseline = rb.compute_baseline([5.0] * 8)   # mad == 0
        self.assertEqual(baseline.mad, 0.0)
        small = rb.evaluate(6.0, baseline)          # 20% over — under the 50% fallback floor
        self.assertFalse(small.is_anomalous)
        self.assertIsNone(small.z)
        big = rb.evaluate(8.0, baseline)             # 60% over — trips the 50% fallback floor
        self.assertTrue(big.is_anomalous)


class WeekdayBucketingTest(unittest.TestCase):
    def test_trailing_same_weekday_dates_is_oldest_first(self):
        # 2026-08-18 is a Tuesday; the target itself is excluded, so the three
        # Tuesdays immediately before it are 2026-07-28, 08-04, 08-11
        got = rb.trailing_same_weekday_dates(date(2026, 8, 18), n=3)
        self.assertEqual(got, [date(2026, 7, 28), date(2026, 8, 4), date(2026, 8, 11)])

    def test_trailing_same_weekday_survives_a_dst_boundary(self):
        # 2026-03-10 is a Tuesday, one day after the 2026-03-08 spring-forward —
        # trailing_same_weekday_dates is pure calendar-week arithmetic (timedelta
        # weeks), which is DST-agnostic by construction; this test locks that in.
        got = rb.trailing_same_weekday_dates(date(2026, 3, 10), n=8)
        self.assertEqual(len(got), 8)
        self.assertTrue(all(d.weekday() == 1 for d in got))  # all Tuesdays
        self.assertEqual(got[-1], date(2026, 3, 3))
