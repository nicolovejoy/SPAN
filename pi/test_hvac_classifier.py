"""Tests for hvac_classifier.py — the Influx I/O + CLI layer over the pure
classifier (#14 sub-project 2).

    cd pi && python3 test_hvac_classifier.py

Nothing here touches the network or a real InfluxDB: query_api/write_api are
mocks, and influxdb_client itself is stubbed out below.
"""
import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

if "influxdb_client" not in sys.modules:
    _ic = types.ModuleType("influxdb_client")
    _ic.InfluxDBClient = object
    # A lambda factory, not the MagicMock class itself: `Point("hvac_mode")`
    # must return a fresh, unrestricted mock. Assigning the class directly
    # would bind "hvac_mode" to MagicMock's `spec` kwarg, which then rejects
    # the `.field(...)` chain (a plain string has no `.field` attribute).
    _ic.Point = lambda *a, **k: mock.MagicMock()
    sys.modules["influxdb_client"] = _ic
    _wa = types.ModuleType("influxdb_client.client.write_api")
    _wa.SYNCHRONOUS = "sync"
    sys.modules["influxdb_client.client.write_api"] = _wa

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import hvac_classifier as hc   # noqa: E402

UTC = timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def interval(start, mode, energy_kwh=0.25):
    return {"start": start, "mode": mode,
            "hp_mean_w": 3000.0, "hp_max_w": 3100.0,
            "aux_mean_w": 0.0, "aux_max_w": 0.0,
            "energy_kwh": energy_kwh, "n_samples": 10}


class _FieldRecorder:
    """Stands in for the Point builder, recording every .field(name, value)."""

    def __init__(self):
        self.fields = {}
        self.tags = {}
        self.times = []
        self.point = mock.MagicMock()
        self.point.field.side_effect = self._field
        self.point.tag.side_effect = self._tag
        self.point.time.side_effect = self._time

    def _field(self, name, value):
        self.fields[name] = value
        return self.point

    def _tag(self, name, value):
        self.tags[name] = value
        return self.point

    def _time(self, when):
        self.times.append(when)
        return self.point


class WriteIntervalsTest(unittest.TestCase):
    def test_writes_one_untagged_point_per_interval_with_mode_energy_split(self):
        write_api = mock.MagicMock()
        iv = {"start": utc(2026, 1, 10, 3, 0), "mode": "heat",
              "hp_mean_w": 3000.0, "hp_max_w": 3100.0,
              "aux_mean_w": 0.0, "aux_max_w": 0.0,
              "energy_kwh": 0.25, "n_samples": 10}
        n = hc.write_intervals(write_api, [iv])
        self.assertEqual(n, 1)
        self.assertEqual(write_api.write.call_count, 1)

    def test_all_intervals_go_out_in_one_batched_write(self):
        # One HTTP round-trip per point would be ~66k of them for the 230-day
        # backfill; the whole batch must go as a single record= list.
        write_api = mock.MagicMock()
        ivs = [interval(utc(2026, 1, 10, 3, 5 * k), "heat") for k in range(12)]
        n = hc.write_intervals(write_api, ivs)
        self.assertEqual(n, 12)
        self.assertEqual(write_api.write.call_count, 1)
        self.assertEqual(len(write_api.write.call_args.kwargs["record"]), 12)

    def test_energy_lands_in_exactly_one_mode_field(self):
        # capture the Point chain: heat interval -> energy_heat_kwh=0.25, others 0.0
        fields = {}
        fake_point = mock.MagicMock()

        def record_field(name, value):
            fields[name] = value
            return fake_point
        fake_point.field.side_effect = record_field
        fake_point.time.return_value = fake_point
        with mock.patch.object(hc, "Point", return_value=fake_point):
            hc.write_intervals(mock.MagicMock(), [{
                "start": utc(2026, 1, 10, 3, 0), "mode": "heat",
                "hp_mean_w": 3000.0, "hp_max_w": 3100.0,
                "aux_mean_w": 0.0, "aux_max_w": 0.0,
                "energy_kwh": 0.25, "n_samples": 10}])
        self.assertEqual(fields["energy_heat_kwh"], 0.25)
        for other in ("energy_cool_kwh", "energy_hot_water_kwh",
                      "energy_idle_kwh", "energy_ambiguous_kwh"):
            self.assertEqual(fields[other], 0.0)
        self.assertEqual(fields["mode"], "heat")
        self.assertEqual(fields["hp_mean_w"], 3000.0)
        self.assertIn("cost_dollars", fields)

    def test_energy_field_follows_the_intervals_own_mode(self):
        # Discriminates against an implementation that hardcodes energy_heat_kwh
        # (which the heat-only test above would pass unchanged).
        for mode in hc.MODES:
            with self.subTest(mode=mode):
                rec = _FieldRecorder()
                with mock.patch.object(hc, "Point", return_value=rec.point):
                    hc.write_intervals(mock.MagicMock(),
                                       [interval(utc(2026, 1, 10, 3, 0), mode, 0.4)])
                self.assertEqual(rec.fields[f"energy_{mode}_kwh"], 0.4)
                self.assertEqual(
                    sorted(k for k in rec.fields if k.startswith("energy_")),
                    sorted(f"energy_{m}_kwh" for m in hc.MODES))
                self.assertEqual(
                    sum(rec.fields[f"energy_{m}_kwh"] for m in hc.MODES), 0.4)

    def test_points_carry_no_tags_and_are_stamped_at_interval_start(self):
        # A tag would split series identity and break overwrite-idempotency;
        # a missing/incorrect .time() would stamp every point at write time.
        rec = _FieldRecorder()
        start = utc(2026, 1, 10, 3, 5)
        with mock.patch.object(hc, "Point", return_value=rec.point):
            hc.write_intervals(mock.MagicMock(), [interval(start, "cool")])
        self.assertEqual(rec.tags, {})
        self.assertEqual(rec.times, [start])

    def test_empty_input_writes_nothing(self):
        write_api = mock.MagicMock()
        self.assertEqual(hc.write_intervals(write_api, []), 0)
        self.assertEqual(write_api.write.call_count, 0)


class MatchBathsTest(unittest.TestCase):
    def test_detected_and_historical_match_within_2h(self):
        detected = [{"start": utc(2026, 1, 10, 3, 0)}]
        historical = [utc(2026, 1, 10, 4, 30)]
        m = hc.match_baths(detected, historical)
        self.assertEqual((m["matched"], m["missed"], m["extra"]), (1, 0, 0))

    def test_unmatched_on_both_sides_counted(self):
        detected = [{"start": utc(2026, 1, 10, 3, 0)}, {"start": utc(2026, 1, 12, 3, 0)}]
        historical = [utc(2026, 1, 10, 3, 30), utc(2026, 1, 14, 9, 0)]
        m = hc.match_baths(detected, historical)
        self.assertEqual((m["matched"], m["missed"], m["extra"]), (1, 1, 1))

    def test_one_historical_bath_absorbs_at_most_one_detection(self):
        # Two detections both inside +/-2h of a single historical bath: a
        # non-consuming matcher would report matched=2, extra=0.
        detected = [{"start": utc(2026, 1, 10, 3, 0)}, {"start": utc(2026, 1, 10, 3, 30)}]
        historical = [utc(2026, 1, 10, 3, 10)]
        m = hc.match_baths(detected, historical)
        self.assertEqual((m["matched"], m["missed"], m["extra"]), (1, 0, 1))
        self.assertEqual(m["extra_times"], [utc(2026, 1, 10, 3, 30)])

    def test_one_detection_absorbs_at_most_one_historical_bath(self):
        # Mirror image of the above: two historical baths both inside +/-2h of
        # a single detection. Without consumption bookkeeping the matcher
        # reports matched=2, missed=0 -- claiming perfect recall from one hit.
        detected = [{"start": utc(2026, 1, 10, 3, 0)}]
        historical = [utc(2026, 1, 10, 2, 30), utc(2026, 1, 10, 3, 30)]
        m = hc.match_baths(detected, historical)
        self.assertEqual((m["matched"], m["missed"], m["extra"]), (1, 1, 0))

    def test_beyond_tolerance_is_not_a_match(self):
        # 2h01m apart -- an implementation with a sloppier window would match.
        detected = [{"start": utc(2026, 1, 10, 3, 0)}]
        historical = [utc(2026, 1, 10, 5, 1)]
        m = hc.match_baths(detected, historical)
        self.assertEqual((m["matched"], m["missed"], m["extra"]), (0, 1, 1))
        self.assertEqual(m["missed_times"], [utc(2026, 1, 10, 5, 1)])
        self.assertEqual(m["extra_times"], [utc(2026, 1, 10, 3, 0)])

    def test_nearest_detection_wins(self):
        # Discriminates "first within tolerance" from "nearest within tolerance":
        # the historical bath sits 90m from the first detection and 10m from the
        # second, so a greedy nearest matcher leaves the FIRST one extra.
        detected = [{"start": utc(2026, 1, 10, 3, 0)}, {"start": utc(2026, 1, 10, 4, 20)}]
        historical = [utc(2026, 1, 10, 4, 30)]
        m = hc.match_baths(detected, historical)
        self.assertEqual((m["matched"], m["missed"], m["extra"]), (1, 0, 1))
        self.assertEqual(m["extra_times"], [utc(2026, 1, 10, 3, 0)])


class _Record:
    def __init__(self, time, values=None, value=None):
        self._time = time
        self.values = values or {}
        self._value = value

    def get_time(self):
        return self._time

    def get_value(self):
        return self._value


class _Table:
    def __init__(self, records):
        self.records = records


class QueryTimelineTest(unittest.TestCase):
    def _query_api(self, records):
        api = mock.MagicMock()
        api.query.return_value = [_Table(records)]
        return api

    def test_pivoted_points_become_interval_dicts_sorted_by_start(self):
        later = _Record(utc(2026, 1, 10, 3, 5), {
            "_time": utc(2026, 1, 10, 3, 5), "mode": "hot_water",
            "hp_mean_w": 3000.0, "hp_max_w": 3100.0,
            "aux_mean_w": 10.0, "aux_max_w": 20.0,
            "energy_heat_kwh": 0.0, "energy_cool_kwh": 0.0,
            "energy_hot_water_kwh": 0.25, "energy_idle_kwh": 0.0,
            "energy_ambiguous_kwh": 0.0})
        earlier = _Record(utc(2026, 1, 10, 3, 0), {
            "_time": utc(2026, 1, 10, 3, 0), "mode": "heat",
            "hp_mean_w": 1000.0, "hp_max_w": 1100.0,
            "aux_mean_w": 0.0, "aux_max_w": 0.0,
            "energy_heat_kwh": 0.08, "energy_cool_kwh": 0.0,
            "energy_hot_water_kwh": 0.0, "energy_idle_kwh": 0.0,
            "energy_ambiguous_kwh": 0.0})
        out = hc.query_timeline(self._query_api([later, earlier]), "-1h")

        self.assertEqual([i["start"] for i in out],
                         [utc(2026, 1, 10, 3, 0), utc(2026, 1, 10, 3, 5)])
        self.assertEqual(out[0], {
            "start": utc(2026, 1, 10, 3, 0), "mode": "heat",
            "hp_mean_w": 1000.0, "hp_max_w": 1100.0,
            "aux_mean_w": 0.0, "aux_max_w": 0.0, "energy_kwh": 0.08})
        # energy_kwh is the sum across the five mode fields, so the nonzero one
        # surfaces regardless of which mode it was.
        self.assertEqual(out[1]["energy_kwh"], 0.25)
        self.assertEqual(out[1]["mode"], "hot_water")

    def test_query_is_pivoted_and_scoped_to_hvac_mode(self):
        api = self._query_api([])
        hc.query_timeline(api, "-3h", "now()")
        flux = api.query.call_args[0][0]
        self.assertIn('_measurement == "hvac_mode"', flux)
        self.assertIn("pivot(", flux)
        self.assertIn("range(start: -3h, stop: now())", flux)


class QueryWeatherTest(unittest.TestCase):
    def test_pads_the_range_and_filters_to_temp_f(self):
        api = mock.MagicMock()
        api.query.return_value = [_Table([_Record(utc(2026, 1, 10, 3, 0), value=41.5)])]
        out = hc.query_weather(api, utc(2026, 1, 10, 4, 0), utc(2026, 1, 10, 8, 0))

        self.assertEqual(out, [{"time": utc(2026, 1, 10, 3, 0), "temp_f": 41.5}])
        flux = api.query.call_args[0][0]
        self.assertIn('_measurement == "weather"', flux)
        self.assertIn('_field == "temp_f"', flux)
        # +/-2h of padding so hvac_modes.temp_at has neighbours at both edges
        self.assertIn("2026-01-10T02:00:00Z", flux)
        self.assertIn("2026-01-10T10:00:00Z", flux)


class ClassifyRangeTest(unittest.TestCase):
    def test_buckets_and_classifies_the_queried_samples(self):
        start, stop = utc(2026, 1, 10, 3, 0), utc(2026, 1, 10, 3, 10)
        # 1500W is below hvac_modes.DHW_MEAN_POWER_MIN_W, so this is space
        # conditioning, not a hot-water reheat -- temperature decides the mode.
        hp = [{"time": start + timedelta(seconds=30 * i), "power": 1500.0} for i in range(20)]

        def fake_circuit(query_api, circuit_name, s, e):
            return hp if circuit_name == hc.HP_CIRCUIT else []

        with mock.patch.object(hc, "query_circuit_power", side_effect=fake_circuit), \
             mock.patch.object(hc, "query_weather",
                               return_value=[{"time": start, "temp_f": 40.0}]):
            out = hc.classify_range(mock.MagicMock(), start, stop)

        self.assertEqual([i["start"] for i in out],
                         [utc(2026, 1, 10, 3, 0), utc(2026, 1, 10, 3, 5)])
        # 40F is below hvac_modes.HEAT_MAX_TEMP_F -> heat, not ambiguous
        self.assertEqual({i["mode"] for i in out}, {"heat"})
        for i in out:
            self.assertAlmostEqual(i["hp_mean_w"], 1500.0)

    def test_range_is_rfc3339_formatted(self):
        with mock.patch.object(hc, "query_circuit_power", return_value=[]) as q, \
             mock.patch.object(hc, "query_weather", return_value=[]):
            hc.classify_range(mock.MagicMock(),
                              utc(2026, 1, 10, 3, 0), utc(2026, 1, 10, 6, 0))
        self.assertEqual(q.call_args[0][2], "2026-01-10T03:00:00Z")
        self.assertEqual(q.call_args[0][3], "2026-01-10T06:00:00Z")


class _FakePanel:
    """A synthetic panel: 30s HP samples across a span, high-power inside the
    given DHW windows and off elsewhere, served back filtered by whatever
    RFC3339 range the module asks for. Outdoor temp is a flat 40F, which is
    below hvac_modes.HEAT_MAX_TEMP_F -- so anything that FAILS the hot-water
    test falls through to `heat`, making a misclassification unmistakable."""

    def __init__(self, base, hours, dhw_windows, temp_f=40.0):
        self.samples = []
        t, end = base, base + timedelta(hours=hours)
        while t < end:
            hot = any(lo <= t < hi for lo, hi in dhw_windows)
            self.samples.append({"time": t, "power": 3200.0 if hot else 0.0})
            t += timedelta(seconds=30)
        self.weather_points = [
            {"time": base - timedelta(hours=2) + timedelta(hours=k), "temp_f": temp_f}
            for k in range(hours + 5)]

    def circuit(self, query_api, name, start, stop):
        if name != hc.HP_CIRCUIT:
            return []
        lo = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        hi = datetime.strptime(stop, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        return [s for s in self.samples if lo <= s["time"] < hi]

    def weather(self, query_api, start, stop):
        return self.weather_points

    def patches(self):
        return (mock.patch.object(hc, "query_circuit_power", side_effect=self.circuit),
                mock.patch.object(hc, "query_weather", side_effect=self.weather))


class RollingWindowContextTest(unittest.TestCase):
    """The rolling --loop window rewrites every interval on each pass, so the
    LAST pass to touch an interval decides its stored label. Without a lead-in
    buffer that last pass is the one with the interval at the window's leading
    edge and therefore the LEAST context -- which truncates the run
    hvac_modes._mark_hot_water measures, and silently relabels the tail of
    every hot-water run. The Phase 0 backtest cannot catch this: backtest
    works in whole-day windows, never the rolling one."""

    # A 30-min hot-water run, 02:30 -> 03:00. Tail interval starts 02:55.
    BASE = utc(2026, 1, 10, 0, 0)
    RUN = (utc(2026, 1, 10, 2, 30), utc(2026, 1, 10, 3, 0))
    TAIL = utc(2026, 1, 10, 2, 55)

    def _writes_at(self, panel, now):
        """Run one --loop pass with the clock frozen at `now`; return the
        interval dicts that pass handed to write_intervals."""
        written = []

        def record(write_api, intervals):
            written.extend(intervals)
            return len(intervals)

        cp, wp = panel.patches()
        with cp, wp, \
             mock.patch.object(hc, "_now", return_value=now), \
             mock.patch.object(hc, "write_intervals", side_effect=record):
            hc.normal_run(mock.MagicMock())
        return written

    def test_run_tail_is_never_written_with_a_truncated_classification(self):
        panel = _FakePanel(self.BASE, 8, [self.RUN])
        # Every pass from just after the run through 3h later -- i.e. every
        # pass whose window can contain the tail interval at all.
        seen = []
        t = utc(2026, 1, 10, 3, 0)
        while t <= utc(2026, 1, 10, 6, 30):
            for iv in self._writes_at(panel, t):
                if iv["start"] == self.TAIL:
                    seen.append((t, iv["mode"]))
            t += timedelta(minutes=5)

        self.assertTrue(seen, "the tail interval was never written by any pass")
        wrong = [(t, m) for t, m in seen if m != "hot_water"]
        self.assertEqual(
            wrong, [],
            f"tail interval written as something other than hot_water: {wrong}")

    def test_leading_edge_pass_writes_nothing_it_cannot_classify_correctly(self):
        # T = 05:55 puts the tail interval exactly at the window's leading
        # edge (window = [02:55, 05:55]). With the lead-in buffer that pass
        # must not write it at all; without one it writes it as `heat`.
        panel = _FakePanel(self.BASE, 8, [self.RUN])
        starts = [iv["start"] for iv in self._writes_at(panel, utc(2026, 1, 10, 5, 55))]
        self.assertNotIn(self.TAIL, starts)
        # ...and the pass is not simply writing nothing.
        self.assertTrue(starts)
        self.assertEqual(min(starts), utc(2026, 1, 10, 3, 55))

    def test_the_last_pass_to_write_the_tail_saw_the_whole_run(self):
        panel = _FakePanel(self.BASE, 8, [self.RUN])
        written = self._writes_at(panel, utc(2026, 1, 10, 4, 55))
        tail = [iv for iv in written if iv["start"] == self.TAIL]
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["mode"], "hot_water")


class DayBoundaryContextTest(unittest.TestCase):
    """backfill/backtest work a UTC day at a time. Midnight UTC is 16:00/17:00
    Pacific -- a plausible bath hour -- so a DHW run can straddle the batch
    boundary. Classified per bare day, each half is a fragment too short to
    pass DHW_RUN_MIN_MINUTES and both get relabelled."""

    # 10 minutes exactly (= DHW_RUN_MIN_MINUTES) split 5/5 across midnight, so
    # BOTH halves fall below the minimum when the run is cut in two.
    RUN = (utc(2026, 1, 10, 23, 55), utc(2026, 1, 11, 0, 5))
    BEFORE = utc(2026, 1, 10, 23, 55)
    AFTER = utc(2026, 1, 11, 0, 0)

    def _day(self, panel, day_start):
        cp, wp = panel.patches()
        with cp, wp:
            return hc.classify_day(mock.MagicMock(), day_start)

    def _panel(self):
        return _FakePanel(utc(2026, 1, 10, 22, 0), 4, [self.RUN])

    def test_run_straddling_midnight_is_hot_water_on_both_sides(self):
        panel = self._panel()
        day10 = {i["start"]: i["mode"] for i in self._day(panel, utc(2026, 1, 10))}
        day11 = {i["start"]: i["mode"] for i in self._day(panel, utc(2026, 1, 11))}
        self.assertEqual(day10[self.BEFORE], "hot_water")
        self.assertEqual(day11[self.AFTER], "hot_water")

    def test_padding_context_is_not_reported_as_part_of_the_day(self):
        # The classified range is wider than the day; the returned intervals
        # must not be. Otherwise consecutive days overlap and backfill writes
        # -- and backtest totals -- double-count the seam.
        panel = self._panel()
        day11 = [i["start"] for i in self._day(panel, utc(2026, 1, 11))]
        self.assertTrue(day11)
        self.assertGreaterEqual(min(day11), utc(2026, 1, 11))
        self.assertLess(max(day11), utc(2026, 1, 12))
        day10 = [i["start"] for i in self._day(panel, utc(2026, 1, 10))]
        self.assertEqual(set(day10) & set(day11), set())

    def test_backfill_and_backtest_both_route_through_classify_day(self):
        # The Phase 0 gate only means anything if backtest reports exactly what
        # backfill would write -- same windowing, same trimming, one function.
        client = mock.MagicMock()
        with mock.patch.object(hc, "classify_day", return_value=[]) as cd, \
             mock.patch.object(hc, "_now", return_value=utc(2026, 1, 12, 6, 0)):
            hc.backfill(client, utc(2026, 1, 10))
            self.assertEqual([c[0][1] for c in cd.call_args_list],
                             [utc(2026, 1, 10), utc(2026, 1, 11)])
            cd.reset_mock()
            hc.backtest(client, 2)
            self.assertEqual([c[0][1] for c in cd.call_args_list],
                             [utc(2026, 1, 10), utc(2026, 1, 11)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
