"""Tests for attribution.py — pure run-grouping + bath predicate over the
classified hvac_mode timeline (#14 sub-project 2).

    cd pi && python3 test_attribution.py

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
import attribution as at   # noqa: E402

UTC = timezone.utc

def utc(*args):
    return datetime(*args, tzinfo=UTC)

def iv(start, mode, hp_mean=3200.0, aux_mean=0.0):
    return {"start": start, "mode": mode, "hp_mean_w": hp_mean, "hp_max_w": hp_mean,
            "aux_mean_w": aux_mean, "aux_max_w": aux_mean,
            "energy_kwh": (hp_mean + aux_mean) * 5 / 60 / 1000, "n_samples": 10}

def series(start, n, mode, **kw):
    return [iv(start + timedelta(minutes=5 * i), mode, **kw) for i in range(n)]

class RunsTest(unittest.TestCase):
    def test_groups_contiguous_same_mode(self):
        ivs = (series(utc(2026, 1, 10, 3, 0), 2, "idle")
               + series(utc(2026, 1, 10, 3, 10), 6, "hot_water")
               + series(utc(2026, 1, 10, 3, 40), 2, "heat"))
        out = at.runs(ivs, "hot_water")
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]), 6)

    def test_gap_breaks_a_run(self):
        ivs = (series(utc(2026, 1, 10, 3, 0), 3, "hot_water")
               + series(utc(2026, 1, 10, 3, 30), 3, "hot_water"))
        self.assertEqual(len(at.runs(ivs, "hot_water")), 2)

    def test_empty(self):
        self.assertEqual(at.runs([], "hot_water"), [])

class BathEventsTest(unittest.TestCase):
    def test_long_hot_water_run_is_a_bath_with_detector_schema(self):
        ivs = series(utc(2026, 1, 10, 3, 0), 6, "hot_water", hp_mean=3200.0, aux_mean=100.0)
        events = at.bath_events(ivs)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["start"], utc(2026, 1, 10, 3, 0))
        self.assertEqual(ev["end"], utc(2026, 1, 10, 3, 30))
        self.assertEqual(ev["duration_min"], 30.0)
        self.assertEqual(ev["hp_mean_power_w"], 3200.0)
        self.assertEqual(ev["hp_max_power_w"], 3200.0)
        self.assertTrue(ev["aux_active"])
        self.assertEqual(ev["aux_mean_power_w"], 100.0)
        # energy: sum of interval energies; cost via rates
        self.assertAlmostEqual(ev["energy_kwh"], 6 * 3300 * 5 / 60 / 1000, places=6)
        self.assertGreater(ev["cost_dollars"], 0)

    def test_short_run_is_not_a_bath(self):
        ivs = series(utc(2026, 1, 10, 3, 0), 4, "hot_water")   # 20 min < 25
        self.assertEqual(at.bath_events(ivs), [])

    def test_weak_run_is_not_a_bath(self):
        ivs = series(utc(2026, 1, 10, 3, 0), 6, "hot_water", hp_mean=2000.0)
        self.assertEqual(at.bath_events(ivs), [])

    def test_aux_inactive_when_aux_near_zero(self):
        ivs = series(utc(2026, 1, 10, 3, 0), 6, "hot_water", aux_mean=0.0)
        self.assertFalse(at.bath_events(ivs)[0]["aux_active"])


if __name__ == "__main__":
    unittest.main()
