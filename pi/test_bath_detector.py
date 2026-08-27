"""Tests for bath_detector.py — re-based onto the hvac_mode timeline
(#14 sub-project 2). run_detection now delegates entirely to
hvac_classifier.query_timeline + attribution.bath_events; no raw circuit
querying or windowing lives here any more.

    cd pi && python3 test_bath_detector.py

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
    # A lambda factory, not the MagicMock class itself: `Point("bath_event")` must
    # return a fresh, unrestricted mock. Assigning the class directly would make
    # the call `Point("bath_event")` bind "bath_event" to MagicMock's `spec` kwarg,
    # which then rejects the `.field(...)` chain bath_detector.py relies on
    # (a plain string has no `.field` attribute for the spec to allow).
    _ic.Point = lambda *a, **k: mock.MagicMock()
    sys.modules["influxdb_client"] = _ic
    _wa = types.ModuleType("influxdb_client.client.write_api")
    _wa.SYNCHRONOUS = "sync"
    sys.modules["influxdb_client.client.write_api"] = _wa

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import bath_detector as bd   # noqa: E402

UTC = timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


class RunDetectionTest(unittest.TestCase):
    def test_detection_reads_the_timeline_not_raw_circuits(self):
        fake_intervals = [
            {"start": utc(2026, 1, 10, 3, 0) + timedelta(minutes=5 * i),
             "mode": "hot_water", "hp_mean_w": 3200.0, "hp_max_w": 3300.0,
             "aux_mean_w": 0.0, "aux_max_w": 0.0,
             "energy_kwh": 3200.0 * 5 / 60 / 1000}
            for i in range(6)
        ]
        with mock.patch.object(bd, "query_timeline", return_value=fake_intervals) as qt:
            events = bd.run_detection(mock.MagicMock(), "-90m")
        qt.assert_called_once()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["duration_min"], 30.0)
        # schema keys write_bath_event consumes must all be present
        for key in ("start", "end", "duration_min", "hp_mean_power_w", "hp_max_power_w",
                    "aux_active", "aux_mean_power_w", "aux_max_power_w",
                    "energy_kwh", "cost_dollars"):
            self.assertIn(key, events[0])

    def test_non_hot_water_intervals_produce_no_event(self):
        # If run_detection still did its own power-threshold windowing (the old
        # raw-circuit path), high hp_mean_w alone would be enough to trigger a
        # detection regardless of mode. Going through attribution.bath_events
        # means only intervals actually labeled "hot_water" count -- a "heat"
        # run at the same power must NOT produce a bath event.
        fake_intervals = [
            {"start": utc(2026, 1, 10, 3, 0) + timedelta(minutes=5 * i),
             "mode": "heat", "hp_mean_w": 3200.0, "hp_max_w": 3300.0,
             "aux_mean_w": 0.0, "aux_max_w": 0.0,
             "energy_kwh": 3200.0 * 5 / 60 / 1000}
            for i in range(6)
        ]
        with mock.patch.object(bd, "query_timeline", return_value=fake_intervals):
            events = bd.run_detection(mock.MagicMock(), "-90m")
        self.assertEqual(events, [])

    def test_old_raw_circuit_path_is_gone(self):
        self.assertFalse(hasattr(bd, "query_circuit_power"))
        self.assertFalse(hasattr(bd, "find_bath_events"))
        self.assertFalse(hasattr(bd, "analyze_window"))
        self.assertFalse(hasattr(bd, "is_bath_like"))
        for const in ("HP_CIRCUIT", "AUX_CIRCUIT", "WINDOW_MINUTES", "STEP_MINUTES",
                      "POWER_THRESHOLD", "DUTY_CYCLE_MIN", "MAX_TRANSITIONS",
                      "MEAN_POWER_MIN"):
            self.assertFalse(hasattr(bd, const), f"{const} should have been deleted")


if __name__ == "__main__":
    unittest.main()
