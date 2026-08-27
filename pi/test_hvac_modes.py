"""Tests for hvac_modes.py — pure HVAC mode classification (#14 sub-project 2).

    cd pi && python3 test_hvac_modes.py

Nothing here touches the network or a real InfluxDB.
"""
import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
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
import hvac_modes as hm   # noqa: E402

UTC = timezone.utc

def utc(*args):
    return datetime(*args, tzinfo=UTC)

def samples(start, seconds_step, powers):
    """[(t0, p0), (t0+step, p1), ...] as sample dicts."""
    return [{"time": start + timedelta(seconds=i * seconds_step), "power": p}
            for i, p in enumerate(powers)]

class BucketIntervalsTest(unittest.TestCase):
    def test_buckets_align_to_5min_boundaries_and_carry_stats(self):
        start = utc(2026, 8, 20, 10, 0)
        hp = samples(start, 30, [3000.0] * 10)          # 10:00:00–10:04:30
        aux = samples(start, 30, [0.0] * 10)
        out = hm.bucket_intervals(hp, aux, start, start + timedelta(minutes=5))
        self.assertEqual(len(out), 1)
        iv = out[0]
        self.assertEqual(iv["start"], utc(2026, 8, 20, 10, 0))
        self.assertEqual(iv["hp_mean_w"], 3000.0)
        self.assertEqual(iv["hp_max_w"], 3000.0)
        self.assertEqual(iv["hp_duty"], 1.0)
        self.assertEqual(iv["hp_transitions"], 0)
        self.assertEqual(iv["aux_mean_w"], 0.0)
        self.assertEqual(iv["n_samples"], 10)
        # (3000 + 0) W * 5 min = 0.25 kWh
        self.assertAlmostEqual(iv["energy_kwh"], 0.25, places=6)

    def test_negative_power_is_absed(self):
        start = utc(2026, 8, 20, 10, 0)
        hp = samples(start, 30, [-3000.0] * 10)
        out = hm.bucket_intervals(hp, [], start, start + timedelta(minutes=5))
        self.assertEqual(out[0]["hp_mean_w"], 3000.0)

    def test_duty_and_transitions_count_crossings_of_idle_threshold(self):
        start = utc(2026, 8, 20, 10, 0)
        # on, on, off, off, on -> duty 3/5, transitions 2 (on->off, off->on)
        hp = samples(start, 60, [3000.0, 3000.0, 0.0, 0.0, 3000.0])
        out = hm.bucket_intervals(hp, [], start, start + timedelta(minutes=5))
        self.assertEqual(out[0]["hp_duty"], 0.6)
        self.assertEqual(out[0]["hp_transitions"], 2)

    def test_gap_interval_is_omitted_not_zeroed(self):
        start = utc(2026, 8, 20, 10, 0)
        hp = samples(start, 30, [3000.0] * 10)  # only the first 5 min has data
        out = hm.bucket_intervals(hp, [], start, start + timedelta(minutes=15))
        self.assertEqual([iv["start"] for iv in out], [utc(2026, 8, 20, 10, 0)])

    def test_unaligned_start_is_floored_to_boundary(self):
        # start 10:02 -> first bucket is 10:00 (samples before `start` excluded anyway)
        start = utc(2026, 8, 20, 10, 2)
        hp = samples(start, 30, [3000.0] * 6)
        out = hm.bucket_intervals(hp, [], start, utc(2026, 8, 20, 10, 5))
        self.assertEqual(out[0]["start"], utc(2026, 8, 20, 10, 0))


if __name__ == "__main__":
    unittest.main()
