# HVAC Mode Timeline (heat/cool/hot-water split) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Classify every 5-minute interval of heat-pump operation as heat / cool / hot_water / idle / ambiguous into a derived `hvac_mode` Influx series (backfilled to 2026-01-04), split the web breakdown's HVAC row into nested sub-rows from it, and re-base `bath_detector.py` onto a generic run-grouping attribution module.

**Architecture:** Timeline-first. Pure classification logic (`pi/hvac_modes.py`) and pure run-grouping (`pi/attribution.py`) with zero I/O, a thin Influx service (`pi/hvac_classifier.py`) mirroring `weather_poller.py`'s CLI shape, and a small web query + splice. Phase 0 (backtest against real data) gates deployment: thresholds in this plan are **seeds**, tuned in Task 5 before anything ships.

**Tech Stack:** Python 3.11 (stdlib `unittest`, `influxdb_client`, no new deps), InfluxDB 2.x/Flux, Next.js 16 + vitest.

**Spec:** `docs/superpowers/specs/2026-08-26-hvac-mode-split-design.md` — read it first; it holds the why, the storage-schema rationale (fields not tags — critical), and the Phase 0 gates.

## Global Constraints

- Timestamps stored in UTC, always. Intervals aligned to wall-clock 5-minute boundaries (epoch // 300s).
- `hvac_mode` points carry **no Influx tags** — tags would split series identity and break overwrite-idempotency (see spec amendment 2026-08-26). Do not add one.
- All power values are `abs()`-ed before use (SPAN reports some circuits negative; house convention, see `bath_detector.analyze_window`).
- Pure modules (`hvac_modes.py`, `attribution.py`) must not import `influxdb_client`, `httpx`, or read env vars.
- Python tests run as `cd pi && python3 test_<name>.py` (stdlib unittest, no pytest, no network — see `test_weather_poller.py` for the influxdb_client stub pattern).
- Web tests: `cd web && npm test` (vitest).
- Every new `pi/*.py` file that a container runs or imports needs a `COPY` line in `pi/Dockerfile` (known deploy gotcha — containers crash-loop without it, see commit 9a0d6f5).
- Threshold constants live in `pi/hvac_modes.py` / `pi/attribution.py` only; nothing else hardcodes them.
- Commit after every green test cycle. Commit messages end with the Claude trailer per session convention.

---

### Task 1: `pi/hvac_modes.py` — interval bucketing

**Files:**
- Create: `pi/hvac_modes.py`
- Test: `pi/test_hvac_modes.py`

**Interfaces:**
- Consumes: nothing (pure; samples are `{"time": datetime, "power": float}` dicts, the exact shape `bath_detector.query_circuit_power` returns today).
- Produces: `bucket_intervals(hp_samples, aux_samples, start, stop) -> list[dict]` where each dict is
  `{"start": datetime, "hp_mean_w": float, "hp_max_w": float, "hp_duty": float, "hp_transitions": int, "aux_mean_w": float, "aux_max_w": float, "energy_kwh": float, "n_samples": int}`.
  Intervals with zero HP samples are **omitted** (a collector gap produces a timeline gap, not invented data). `INTERVAL_MINUTES = 5`, `IDLE_POWER_W = 50.0` also exported.

- [ ] **Step 1: Write the failing tests**

Create `pi/test_hvac_modes.py`. Copy the `influxdb_client` stub preamble from `pi/test_weather_poller.py` lines 8–26 verbatim (harmless here — `hvac_modes` imports nothing, but Task 4's test file will extend this one's helpers), then:

```python
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
```

Include a `if __name__ == "__main__": unittest.main()` footer (match `test_weather_poller.py`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 test_hvac_modes.py`
Expected: `ModuleNotFoundError: No module named 'hvac_modes'` (or AttributeError once the file exists empty).

- [ ] **Step 3: Implement `bucket_intervals`**

Create `pi/hvac_modes.py`:

```python
#!/usr/bin/env python3
"""Pure HVAC mode classification — no I/O, no env, no influx imports.

Turns raw 30s HP + aux power samples plus hourly outdoor temperature into a
labeled 5-minute timeline: heat / cool / hot_water / idle / ambiguous.
Thresholds below are Phase 0 SEEDS; see the findings note referenced next to
each constant once Task 5 has tuned them against January-to-now data.
"""
from datetime import datetime, timedelta, timezone

INTERVAL_MINUTES = 5
IDLE_POWER_W = 50.0            # on/off boundary, same as bath_detector's POWER_THRESHOLD

# --- DHW (hot water) signature seeds — season-invariant, Phase 0-tunable ---
DHW_MEAN_POWER_MIN_W = 2500.0  # seeded from bath_detector.MEAN_POWER_MIN
DHW_DUTY_MIN = 0.85            # seeded from bath_detector.DUTY_CYCLE_MIN
DHW_MAX_TRANSITIONS = 2        # seeded from bath_detector.MAX_TRANSITIONS
DHW_RUN_MIN_MINUTES = 10       # shorter high-power runs -> not a reheat
DHW_RUN_MAX_MINUTES = 120      # longer -> sustained space conditioning

# --- heat vs cool temperature bands (deg F), Phase 0-tunable ---
HEAT_MAX_TEMP_F = 58.0
COOL_MIN_TEMP_F = 68.0
WEATHER_MAX_STALENESS_MIN = 90


def _floor_to_interval(dt: datetime) -> datetime:
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - epoch % (INTERVAL_MINUTES * 60), tz=timezone.utc)


def _stats(powers: list[float]) -> dict:
    above = [p > IDLE_POWER_W for p in powers]
    transitions = sum(1 for i in range(1, len(above)) if above[i] != above[i - 1])
    return {
        "mean": sum(powers) / len(powers) if powers else 0.0,
        "max": max(powers) if powers else 0.0,
        "duty": sum(above) / len(above) if above else 0.0,
        "transitions": transitions,
    }


def bucket_intervals(hp_samples: list[dict], aux_samples: list[dict],
                     start: datetime, stop: datetime) -> list[dict]:
    """Aggregate raw samples into aligned 5-min interval stats. Intervals with
    zero HP samples are omitted: a collector gap must surface as a timeline
    gap, never as invented zeros."""
    width = timedelta(minutes=INTERVAL_MINUTES)
    hp_by_bucket: dict[datetime, list[float]] = {}
    aux_by_bucket: dict[datetime, list[float]] = {}
    for target, source in ((hp_by_bucket, hp_samples), (aux_by_bucket, aux_samples)):
        for s in source:
            if start <= s["time"] < stop:
                target.setdefault(_floor_to_interval(s["time"]), []).append(abs(s["power"]))

    out = []
    b = _floor_to_interval(start)
    while b < stop:
        hp = hp_by_bucket.get(b, [])
        if hp:
            h, a = _stats(hp), _stats(aux_by_bucket.get(b, []))
            out.append({
                "start": b,
                "hp_mean_w": h["mean"], "hp_max_w": h["max"],
                "hp_duty": h["duty"], "hp_transitions": h["transitions"],
                "aux_mean_w": a["mean"], "aux_max_w": a["max"],
                "energy_kwh": (h["mean"] + a["mean"]) * INTERVAL_MINUTES / 60 / 1000,
                "n_samples": len(hp),
            })
        b += width
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_hvac_modes.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add pi/hvac_modes.py pi/test_hvac_modes.py
git commit -m "hvac_modes: pure 5-min interval bucketing (#14 sub-project 2)"
```

---

### Task 2: `pi/hvac_modes.py` — classification

**Files:**
- Modify: `pi/hvac_modes.py`
- Test: `pi/test_hvac_modes.py`

**Interfaces:**
- Consumes: interval dicts from `bucket_intervals` (Task 1); weather points `{"time": datetime, "temp_f": float}` (the shape `weather_poller._parse_hourly_response` produces and the `weather` measurement stores).
- Produces: `classify(intervals, weather_points) -> list[dict]` — same dicts with `"mode"` added, one of `"heat" | "cool" | "hot_water" | "idle" | "ambiguous"`. Also `temp_at(weather_points, dt) -> float | None` (nearest reading within `WEATHER_MAX_STALENESS_MIN`, else None).

- [ ] **Step 1: Write the failing tests**

Append to `pi/test_hvac_modes.py`:

```python
def iv(start, hp_mean, duty=1.0, transitions=0, aux_mean=0.0):
    """Interval dict factory with sane defaults for classification tests."""
    return {"start": start, "hp_mean_w": hp_mean, "hp_max_w": hp_mean,
            "hp_duty": duty, "hp_transitions": transitions,
            "aux_mean_w": aux_mean, "aux_max_w": aux_mean,
            "energy_kwh": (hp_mean + aux_mean) * 5 / 60 / 1000, "n_samples": 10}

def series(start, n, **kw):
    return [iv(start + timedelta(minutes=5 * i), **kw) for i in range(n)]

COLD = [{"time": utc(2026, 1, 10, h), "temp_f": 35.0} for h in range(24)]
HOT = [{"time": utc(2026, 7, 10, h), "temp_f": 85.0} for h in range(24)]
MILD = [{"time": utc(2026, 4, 10, h), "temp_f": 62.0} for h in range(24)]

class TempAtTest(unittest.TestCase):
    def test_nearest_reading_within_staleness_window(self):
        self.assertEqual(hm.temp_at(COLD, utc(2026, 1, 10, 3, 20)), 35.0)

    def test_none_when_no_reading_close_enough(self):
        self.assertIsNone(hm.temp_at(COLD, utc(2026, 1, 12, 3)))

    def test_none_on_empty(self):
        self.assertIsNone(hm.temp_at([], utc(2026, 1, 10, 3)))

class ClassifyTest(unittest.TestCase):
    def test_low_power_is_idle_regardless_of_weather(self):
        out = hm.classify(series(utc(2026, 1, 10, 3), 3, hp_mean=10.0), COLD)
        self.assertEqual([i["mode"] for i in out], ["idle"] * 3)

    def test_sustained_high_power_on_cold_day_is_heat(self):
        # 3 hours of steady 3kW: too long for a DHW reheat -> space heating
        out = hm.classify(series(utc(2026, 1, 10, 3), 36, hp_mean=3000.0), COLD)
        self.assertEqual({i["mode"] for i in out}, {"heat"})

    def test_sustained_high_power_on_hot_day_is_cool(self):
        out = hm.classify(series(utc(2026, 7, 10, 3), 36, hp_mean=3000.0), HOT)
        self.assertEqual({i["mode"] for i in out}, {"cool"})

    def test_bounded_high_power_run_is_hot_water_in_any_season(self):
        for weather, month in ((COLD, 1), (HOT, 7)):
            # 30 min of DHW-shaped draw framed by idle
            run = (series(utc(2026, month, 10, 3, 0), 2, hp_mean=10.0)
                   + series(utc(2026, month, 10, 3, 10), 6, hp_mean=3200.0)
                   + series(utc(2026, month, 10, 3, 40), 2, hp_mean=10.0))
            out = hm.classify(run, weather)
            self.assertEqual([i["mode"] for i in out],
                             ["idle"] * 2 + ["hot_water"] * 6 + ["idle"] * 2,
                             f"month={month}")

    def test_short_high_power_blip_is_not_hot_water(self):
        # one 5-min interval < DHW_RUN_MIN_MINUTES -> falls through to temp
        run = (series(utc(2026, 1, 10, 3, 0), 1, hp_mean=10.0)
               + series(utc(2026, 1, 10, 3, 5), 1, hp_mean=3200.0)
               + series(utc(2026, 1, 10, 3, 10), 1, hp_mean=10.0))
        out = hm.classify(run, COLD)
        self.assertEqual(out[1]["mode"], "heat")

    def test_choppy_high_power_is_not_hot_water(self):
        # duty/transition profile fails the DHW gate -> temp decides
        out = hm.classify(series(utc(2026, 1, 10, 3), 6, hp_mean=3000.0,
                                 duty=0.5, transitions=6), COLD)
        self.assertEqual({i["mode"] for i in out}, {"heat"})

    def test_midband_temp_is_ambiguous(self):
        out = hm.classify(series(utc(2026, 4, 10, 3), 36, hp_mean=3000.0), MILD)
        self.assertEqual({i["mode"] for i in out}, {"ambiguous"})

    def test_missing_weather_is_ambiguous_but_dhw_still_labels(self):
        run = (series(utc(2026, 1, 10, 3, 0), 36, hp_mean=3000.0)      # would-be heat
               + series(utc(2026, 1, 10, 6, 0), 1, hp_mean=10.0)
               + series(utc(2026, 1, 10, 6, 5), 6, hp_mean=3200.0)     # DHW shape
               + series(utc(2026, 1, 10, 6, 35), 1, hp_mean=10.0))
        out = hm.classify(run, [])
        self.assertEqual({i["mode"] for i in out[:36]}, {"ambiguous"})
        self.assertEqual([i["mode"] for i in out[37:43]], ["hot_water"] * 6)

    def test_timeline_gap_splits_a_dhw_run(self):
        # 6 DHW-shaped intervals but a 20-min hole in the middle: the two
        # halves are separate runs, each under DHW_RUN_MIN_MINUTES's 2-interval
        # floor only if shorter — here each half is 15 min, still >= 10 -> both hot_water
        run = (series(utc(2026, 1, 10, 3, 0), 3, hp_mean=3200.0)
               + series(utc(2026, 1, 10, 3, 35), 3, hp_mean=3200.0))
        out = hm.classify(run, COLD)
        self.assertEqual({i["mode"] for i in out}, {"hot_water"})
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `cd pi && python3 test_hvac_modes.py`
Expected: Task 1 tests PASS, new tests FAIL with `AttributeError: ... no attribute 'classify'`.

- [ ] **Step 3: Implement `temp_at` and `classify`**

Append to `pi/hvac_modes.py`:

```python
def temp_at(weather_points: list[dict], dt: datetime) -> float | None:
    """Nearest hourly reading within WEATHER_MAX_STALENESS_MIN, else None."""
    best, best_gap = None, None
    for w in weather_points:
        gap = abs((w["time"] - dt).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = w, gap
    if best is None or best_gap > WEATHER_MAX_STALENESS_MIN * 60:
        return None
    return best["temp_f"]


def _is_dhw_shaped(interval: dict) -> bool:
    return (interval["hp_mean_w"] >= DHW_MEAN_POWER_MIN_W
            and interval["hp_duty"] >= DHW_DUTY_MIN
            and interval["hp_transitions"] <= DHW_MAX_TRANSITIONS)


def _mark_hot_water(intervals: list[dict]) -> set[datetime]:
    """Group consecutive DHW-shaped intervals; runs whose duration lands in
    [DHW_RUN_MIN_MINUTES, DHW_RUN_MAX_MINUTES] are hot water. 'Consecutive'
    means exactly INTERVAL_MINUTES apart — a timeline gap breaks the run.
    Over-long runs fall through to temperature classification (sustained
    space conditioning)."""
    starts: set[datetime] = set()
    run: list[dict] = []

    def flush():
        if run:
            minutes = len(run) * INTERVAL_MINUTES
            if DHW_RUN_MIN_MINUTES <= minutes <= DHW_RUN_MAX_MINUTES:
                starts.update(i["start"] for i in run)
        run.clear()

    prev = None
    for i in intervals:
        contiguous = prev is not None and (i["start"] - prev) == timedelta(minutes=INTERVAL_MINUTES)
        if _is_dhw_shaped(i):
            if not contiguous:
                flush()
            run.append(i)
        else:
            flush()
        prev = i["start"]
    flush()
    return starts


def classify(intervals: list[dict], weather_points: list[dict]) -> list[dict]:
    """Label each interval with its mode. Stage 1: season-invariant DHW shape.
    Stage 2: remaining active intervals split by outdoor temperature."""
    hot_water = _mark_hot_water(intervals)
    out = []
    for i in intervals:
        total = i["hp_mean_w"] + i["aux_mean_w"]
        if total < IDLE_POWER_W:
            mode = "idle"
        elif i["start"] in hot_water:
            mode = "hot_water"
        else:
            t = temp_at(weather_points, i["start"])
            if t is None:
                mode = "ambiguous"
            elif t <= HEAT_MAX_TEMP_F:
                mode = "heat"
            elif t >= COOL_MIN_TEMP_F:
                mode = "cool"
            else:
                mode = "ambiguous"
        out.append({**i, "mode": mode})
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_hvac_modes.py`
Expected: all PASS. If `test_timeline_gap_splits_a_dhw_run` fails, check the contiguity handling in `_mark_hot_water` — a gap must `flush()` before appending.

- [ ] **Step 5: Commit**

```bash
git add pi/hvac_modes.py pi/test_hvac_modes.py
git commit -m "hvac_modes: two-stage mode classification (#14 sub-project 2)"
```

---

### Task 3: `pi/attribution.py` — runs and the bath predicate

**Files:**
- Create: `pi/attribution.py`
- Test: `pi/test_attribution.py`

**Interfaces:**
- Consumes: classified interval dicts (Task 2 output — must include `mode`, `start`, `hp_mean_w`, `hp_max_w`, `aux_mean_w`, `aux_max_w`, `energy_kwh`); `rates.cost_for_kwh(kwh: float, dt: datetime) -> float`.
- Produces:
  - `runs(intervals, mode) -> list[list[dict]]` — maximal groups of contiguous (exactly 5 min apart) intervals of `mode`.
  - `bath_events(intervals) -> list[dict]` — dicts with the **exact keys `bath_detector.write_bath_event` consumes today**: `start`, `end`, `duration_min`, `hp_mean_power_w`, `hp_max_power_w`, `aux_active`, `aux_mean_power_w`, `aux_max_power_w`, `energy_kwh`, `cost_dollars`.
  - Constants `BATH_MIN_MINUTES = 25`, `BATH_MEAN_POWER_MIN_W = 2500.0`.

- [ ] **Step 1: Write the failing tests**

Create `pi/test_attribution.py` (same stub preamble + `utc`/`iv`/`series` helpers as `test_hvac_modes.py` — copy them in; each test file stands alone). `iv` here additionally takes `mode`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 test_attribution.py`
Expected: `ModuleNotFoundError: No module named 'attribution'`.

- [ ] **Step 3: Implement**

Create `pi/attribution.py`:

```python
#!/usr/bin/env python3
"""Pure run-grouping over a classified hvac_mode timeline — no I/O.

An event detector is a predicate over runs. One ships today (bath);
shower / laundry hot-water predicates are future one-liners here (#14/#17)."""
from datetime import timedelta

from hvac_modes import INTERVAL_MINUTES
from rates import cost_for_kwh

BATH_MIN_MINUTES = 25          # bath_detector required >= 3 overlapping 15-min windows
BATH_MEAN_POWER_MIN_W = 2500.0


def runs(intervals: list[dict], mode: str) -> list[list[dict]]:
    """Maximal groups of contiguous (exactly INTERVAL_MINUTES apart)
    intervals labeled `mode`. A timeline gap breaks a run."""
    out: list[list[dict]] = []
    current: list[dict] = []
    step = timedelta(minutes=INTERVAL_MINUTES)
    for i in intervals:
        if i["mode"] != mode:
            if current:
                out.append(current)
                current = []
            continue
        if current and i["start"] - current[-1]["start"] != step:
            out.append(current)
            current = []
        current.append(i)
    if current:
        out.append(current)
    return out


def bath_events(intervals: list[dict]) -> list[dict]:
    """hot_water runs meeting duration + power bounds, shaped exactly like
    bath_detector's historical event dicts so write_bath_event and the ±2h
    dedup keep working unchanged."""
    events = []
    for run in runs(intervals, "hot_water"):
        duration_min = len(run) * INTERVAL_MINUTES
        hp_mean = sum(i["hp_mean_w"] for i in run) / len(run)
        if duration_min < BATH_MIN_MINUTES or hp_mean < BATH_MEAN_POWER_MIN_W:
            continue
        start = run[0]["start"]
        end = run[-1]["start"] + timedelta(minutes=INTERVAL_MINUTES)
        aux_mean = sum(i["aux_mean_w"] for i in run) / len(run)
        energy_kwh = sum(i["energy_kwh"] for i in run)
        events.append({
            "start": start,
            "end": end,
            "duration_min": float(duration_min),
            "hp_mean_power_w": round(hp_mean, 1),
            "hp_max_power_w": round(max(i["hp_max_w"] for i in run), 1),
            "aux_active": any(i["aux_mean_w"] > 50.0 for i in run),
            "aux_mean_power_w": round(aux_mean, 1),
            "aux_max_power_w": round(max(i["aux_max_w"] for i in run), 1),
            "energy_kwh": round(energy_kwh, 3),
            "cost_dollars": round(cost_for_kwh(energy_kwh, start), 2),
        })
    return events
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_attribution.py`
Expected: all PASS. Also re-run `python3 test_hvac_modes.py` (shared import surface).

- [ ] **Step 5: Commit**

```bash
git add pi/attribution.py pi/test_attribution.py
git commit -m "attribution: run grouping + bath predicate over the mode timeline (#14 sub-project 2)"
```

---

### Task 4: `pi/hvac_classifier.py` — Influx I/O service + CLI

**Files:**
- Create: `pi/hvac_classifier.py`
- Modify: `pi/Dockerfile` (COPY lines)
- Test: `pi/test_hvac_classifier.py`

**Interfaces:**
- Consumes: `hvac_modes.bucket_intervals` / `classify`; `attribution.bath_events` (for `--backtest --compare-baths`).
- Produces (Task 6 depends on these exact names):
  - `query_timeline(query_api, start: str, stop: str = "now()") -> list[dict]` — reads `hvac_mode` points back into classified-interval dicts (keys: `start`, `mode`, `hp_mean_w`, `hp_max_w`, `aux_mean_w`, `aux_max_w`, `energy_kwh`).
  - `classify_range(query_api, start: datetime, stop: datetime) -> list[dict]` — query raw + weather, bucket, classify.
  - `write_intervals(write_api, intervals) -> int`.
  - CLI: `--loop [--interval 600]`, `--backfill [--start-date 2026-01-04]`, `--backtest [--days N] [--compare-baths]`.

- [ ] **Step 1: Write the failing tests**

Create `pi/test_hvac_classifier.py` with the stub preamble, then (mock `query_api`/`write_api` in the style of `test_weather_poller.py`'s write tests):

```python
import hvac_classifier as hc   # noqa: E402

class WriteIntervalsTest(unittest.TestCase):
    def test_writes_one_untagged_point_per_interval_with_mode_energy_split(self):
        write_api = mock.MagicMock()
        interval = {"start": utc(2026, 1, 10, 3, 0), "mode": "heat",
                    "hp_mean_w": 3000.0, "hp_max_w": 3100.0,
                    "aux_mean_w": 0.0, "aux_max_w": 0.0,
                    "energy_kwh": 0.25, "n_samples": 10}
        n = hc.write_intervals(write_api, [interval])
        self.assertEqual(n, 1)
        self.assertEqual(write_api.write.call_count, 1)

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 test_hvac_classifier.py`
Expected: `ModuleNotFoundError: No module named 'hvac_classifier'`.

- [ ] **Step 3: Implement the service**

Create `pi/hvac_classifier.py`. Structure (write it in full — the Flux/query helpers copy established patterns from `bath_detector.query_circuit_power` and `daily_report`):

```python
#!/usr/bin/env python3
"""HVAC mode timeline service: classify 5-min intervals into the hvac_mode
measurement. Pure logic lives in hvac_modes.py; this file is Influx I/O + CLI.

Idempotency: hvac_mode points carry NO tags, so a rewrite at the same
timestamp overwrites in place (see weather_poller.write_weather_points for
why a tag would silently break this). The --loop pass re-classifies the
trailing 3h every pass, which self-heals missed passes and late data."""
import argparse
import os
import time
import logging
from datetime import datetime, timezone, timedelta

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

import attribution
import hvac_modes
from rates import cost_for_kwh

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "home")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "span")

HP_CIRCUIT = "Heat pump (HP)"
AUX_CIRCUIT = "Auxiliary / Heat pump (HP)"
TRAILING_WINDOW_HOURS = 3
MODES = ("heat", "cool", "hot_water", "idle", "ambiguous")
```

Then implement, in order:

1. `query_circuit_power(query_api, circuit_name, start, stop)` — copy `bath_detector.query_circuit_power` verbatim (it moves here; Task 6 deletes the original).
2. `query_weather(query_api, start, stop) -> list[dict]` — Flux over `_measurement == "weather"`, `_field == "temp_f"`, returning `{"time", "temp_f"}` dicts. Pad the queried range by ±2h so `temp_at` has neighbors at the edges.
3. `classify_range(query_api, start, stop)` — RFC3339-format start/stop (`.strftime("%Y-%m-%dT%H:%M:%SZ")`), query both circuits + weather, `hvac_modes.bucket_intervals(...)` then `hvac_modes.classify(...)`.
4. `write_intervals(write_api, intervals)` — per interval, one `Point("hvac_mode")` with fields: the five `energy_<mode>_kwh` floats (interval's `energy_kwh` into its own mode's field, `0.0` into the other four — always write all five so field types/coverage stay uniform), `mode` (string), `hp_mean_w`, `hp_max_w`, `aux_mean_w`, `aux_max_w`, `cost_dollars` (= `cost_for_kwh(energy_kwh, start)`), `.time(interval["start"])`. **No tags.** Return count.
5. `query_timeline(query_api, start, stop="now()")` — Flux with `pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")`, one output dict per point with keys `start` (the record's `_time`), `mode`, `hp_mean_w`, `hp_max_w`, `aux_mean_w`, `aux_max_w`, and `energy_kwh` = sum of the five `energy_*_kwh` columns (only one is nonzero). Sort by `start`.
6. `match_baths(detected, historical_starts)` — greedy nearest-match within ±2h (mirrors `event_already_exists`'s tolerance): returns `{"matched": int, "missed": int, "extra": int, "missed_times": [...], "extra_times": [...]}` where `missed` = historical baths not detected, `extra` = detections with no historical counterpart.
7. `normal_run(client)` — `stop` = now floored to the 5-min boundary, `start = stop - 3h`; classify_range then write_intervals; log per-mode interval counts.
8. `backfill(client, start_date)` — day by day from `start_date` through yesterday (UTC), classify + write each day, log a per-day one-liner: `2026-01-05: heat 41.2 kWh cool 0.0 hot_water 3.1 idle 0.4 ambiguous 1.7 (n=270)`.
9. `backtest(client, days, compare_baths)` — like backfill over the last `days` days but print, never write. With `compare_baths`: also run `attribution.bath_events` on each day's intervals, query historical `bath_event` starts for the same range (Flux: `_measurement == "bath_event"`, `_field == "duration_min"`), and print `match_baths` totals plus every `missed_times`/`extra_times` entry (Phase 0 examines each diff individually).
10. `main()` — argparse exactly like `weather_poller.main()` plus `--backtest --days N --compare-baths`; token guard; loop mode wraps `normal_run` in try/except with `time.sleep(args.interval)` (default 600).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_hvac_classifier.py && python3 test_hvac_modes.py && python3 test_attribution.py`
Expected: all PASS.

- [ ] **Step 5: Add Dockerfile COPY lines**

In `pi/Dockerfile`, after `COPY bath_detector.py .` add:

```dockerfile
COPY hvac_modes.py .
COPY attribution.py .
COPY hvac_classifier.py .
```

(Compose service block comes in Task 7, after Phase 0 clears deployment.)

- [ ] **Step 6: Commit**

```bash
git add pi/hvac_classifier.py pi/test_hvac_classifier.py pi/Dockerfile
git commit -m "hvac_classifier: timeline service with loop/backfill/backtest (#14 sub-project 2)"
```

---

### Task 5: Phase 0 — backtest against real data, tune thresholds **[GATE]**

This task talks to the real InfluxDB on the Pi and involves judgment. It is **not** delegable to a context-free subagent as a mechanical step — run it in the main session (or a subagent with this full task + spec as its brief), and involve Nico if gates won't converge.

**Files:**
- Modify: `pi/hvac_modes.py`, `pi/attribution.py` (constants only)
- Create: `docs/superpowers/notes/2026-XX-XX-hvac-phase0-findings.md`

**Interfaces:**
- Consumes: `hvac_classifier.py --backtest` (Task 4).
- Produces: tuned threshold constants + a findings note documenting where each number came from. **Deployment (Task 7) is blocked until the three gates pass.**

- [ ] **Step 1: Sync the code to the Pi and run a wide backtest**

The Pi has the production `.env` (secrets never leave it). Check the Pi checkout is on `main` and current first (known gotcha — it can sit on a stale branch):

```bash
ssh nico@phrpi.local 'cd ~/span && git fetch && git checkout main && git pull --rebase && git log --oneline -1'
ssh nico@phrpi.local 'cd ~/span/pi && set -a && . ./.env && set +a && python3 hvac_classifier.py --backtest --days 200 --compare-baths' | tee /tmp/phase0-backtest.txt
```

(If the Pi checkout lives elsewhere, `ssh nico@phrpi.local 'ls ~'` to find it; `status.sh` output names it too. If system python on the Pi lacks `influxdb_client`, run inside the collector image instead: `docker compose -f ~/span/pi/docker-compose.yml run --rm --entrypoint python collector hvac_classifier.py --backtest ...` — but plain python3 is likely fine, the backup scripts run that way.)

- [ ] **Step 2: Evaluate the three gates**

1. **Bath parity ≥ 95%:** from the `--compare-baths` totals: `matched / (matched + missed)`. Examine EVERY missed/extra timestamp — pull the raw window in Grafana (https://span.pianohouseproject.org or the Pi Grafana) or with a quick Flux query, and decide whether the old detector or the new classifier is right. The old detector is not ground truth; a justified diff is fine if the findings note records the justification.
2. **Seasonal sanity:** in the per-day mode totals — `heat` ≈ 0 kWh on the hottest July days; `cool` ≈ 0 kWh in deep January. "≈ 0" = under 1 kWh/day.
3. **Energy conservation within 2%:** for ~5 sampled gap-free days across seasons, per-day sum over all five modes vs. the HP+aux circuit energy for that day. Get the reference number from the existing rollups (`circuit_1h`, `energy_wh_counter` summed for the two HP circuits) — a small throwaway Flux query, fine to run ad hoc.

- [ ] **Step 3: Tune and iterate**

Adjust only the named constants in `hvac_modes.py` (`DHW_*`, `HEAT_MAX_TEMP_F`, `COOL_MIN_TEMP_F`) and `attribution.py` (`BATH_*`). After each change: re-run the unit tests locally (they must stay green — if a tuned constant breaks a synthetic test, update the test's synthetic data to match the new constant and say so in the commit), re-run the backtest. Watch the `ambiguous` share — over ~10% of active energy across the year means the temp bands are too wide.

**If DHW proves inseparable in winter** (parity can't approach the gate without wrecking seasonal sanity): STOP. This is the spec's designed bail-out point — report findings to Nico and redesign; do not deploy a classifier that doesn't classify.

- [ ] **Step 4: Write the findings note**

Create `docs/superpowers/notes/2026-XX-XX-hvac-phase0-findings.md` (real date): final constants table with one line each on where the value came from; gate results (parity %, the examined diffs and their verdicts; seasonal numbers; conservation deltas); the ambiguous-share number; anything surprising.

- [ ] **Step 5: Commit**

```bash
git add pi/hvac_modes.py pi/attribution.py pi/test_hvac_modes.py pi/test_attribution.py docs/superpowers/notes/
git commit -m "hvac: Phase 0 backtest — tuned thresholds + findings (#14 sub-project 2)"
```

---

### Task 6: Re-base `bath_detector.py` onto the timeline

**Files:**
- Modify: `pi/bath_detector.py`
- Test: `pi/test_bath_detector.py` (new)

**Interfaces:**
- Consumes: `hvac_classifier.query_timeline`, `attribution.bath_events`.
- Produces: unchanged externals — same `bath_event` schema, same ±2h dedup, same CLI (`--backtest/--backfill/--days/--loop/--interval`). `daily_report.py` needs zero changes.

- [ ] **Step 1: Write the failing test**

Create `pi/test_bath_detector.py` (stub preamble as before):

```python
import bath_detector as bd   # noqa: E402

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

    def test_old_raw_circuit_path_is_gone(self):
        self.assertFalse(hasattr(bd, "query_circuit_power"))
        self.assertFalse(hasattr(bd, "find_bath_events"))
        self.assertFalse(hasattr(bd, "analyze_window"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd pi && python3 test_bath_detector.py`
Expected: FAIL (`run_detection` still queries circuits; old functions still exist).

- [ ] **Step 3: Re-base the detector**

In `pi/bath_detector.py`:

- **Delete** `query_circuit_power`, `analyze_window`, `is_bath_like`, `find_bath_events` and the constants `HP_CIRCUIT`, `AUX_CIRCUIT`, `WINDOW_MINUTES`, `STEP_MINUTES`, `POWER_THRESHOLD`, `DUTY_CYCLE_MIN`, `MAX_TRANSITIONS`, `MEAN_POWER_MIN` (the logic now lives in `hvac_modes`/`attribution`; delete, don't flag off — spec's definition of done).
- Add imports: `from hvac_classifier import query_timeline` and `import attribution`.
- Replace `run_detection` with:

```python
def run_detection(query_api, start: str, stop: str = "now()") -> list[dict]:
    """Detect baths from the hvac_mode timeline (written by hvac_classifier)."""
    intervals = query_timeline(query_api, start, stop)
    logger.info(f"Queried {len(intervals)} timeline intervals")
    return attribution.bath_events(intervals)
```

- `event_already_exists`, `write_bath_event`, `backtest`, `normal_run`, `main` stay as they are (they only call `run_detection` and the write/dedup helpers). Keep `LOOKBACK_MINUTES = 90` — the classifier's 3h trailing window comfortably covers it.

- [ ] **Step 4: Run all pi tests**

Run: `cd pi && python3 test_bath_detector.py && python3 test_hvac_modes.py && python3 test_attribution.py && python3 test_hvac_classifier.py && python3 test_weather_poller.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add pi/bath_detector.py pi/test_bath_detector.py
git commit -m "bath_detector: re-base onto hvac_mode timeline, delete raw path (#14 sub-project 2)"
```

---

### Task 7: Deploy to the Pi + backfill + verify

Ops task — main session, not a code subagent. Needs `ssh nico@phrpi.local`.

**Files:**
- Modify: `pi/docker-compose.yml`

**Interfaces:**
- Consumes: everything above; Phase 0 gates must be green.
- Produces: `hvac_mode` live and backfilled to 2026-01-04 — what Task 8's web queries read.

- [ ] **Step 1: Add the compose service**

In `pi/docker-compose.yml`, after the `weather:` block, mirroring it exactly:

```yaml
  hvac-classifier:
    build: .
    container_name: hvac-classifier
    restart: unless-stopped
    command: ["python", "-u", "hvac_classifier.py", "--loop"]
    env_file:
      - .env
    environment:
      - INFLUXDB_URL=http://influxdb:8086
      - INFLUXDB_TOKEN=${INFLUXDB_TOKEN}
      - INFLUXDB_ORG=home
      - INFLUXDB_BUCKET=span
    depends_on:
      - influxdb
```

Commit (message: `pi: hvac-classifier compose service (#14 sub-project 2)`), push to origin.

- [ ] **Step 2: Deploy and start**

```bash
ssh nico@phrpi.local 'cd ~/span && git pull --rebase && cd pi && docker compose build hvac-classifier bath-detector && docker compose up -d hvac-classifier bath-detector'
ssh nico@phrpi.local 'cd ~/span/pi && docker compose logs --tail 20 hvac-classifier bath-detector'
```

Expected: classifier logs a normal_run pass with per-mode counts; bath-detector logs "Queried N timeline intervals" (not the old "HP samples" line). If either crash-loops on an import, a Dockerfile COPY line is missing — that's the known failure shape (9a0d6f5).

- [ ] **Step 3: Backfill**

```bash
ssh nico@phrpi.local 'cd ~/span/pi && docker compose run --rm hvac-classifier python -u hvac_classifier.py --backfill --start-date 2026-01-04'
```

~230 days, one query batch per day; expect minutes-to-tens-of-minutes. Watch the per-day log lines for anomalies (days with 0 intervals = collector gaps; cross-check a couple against the known-gap days from the daily gap emails).

- [ ] **Step 4: Verify the series**

Flux spot-checks (via `ssh` + `influx` CLI in the influxdb container, or Grafana Explore):

1. Interval count: `hvac_mode` points for a recent full day ≈ 288 minus known gaps.
2. Conservation re-check on live data: one gap-free day's five-field energy sum vs. HP+aux `circuit_1h` counter sum — within 2%.
3. `bath_event` continuity: after a real bath occurs post-deploy, confirm a new `bath_event` point with sane numbers (this may take a day — note it in the handoff rather than blocking).

- [ ] **Step 5: Commit any fixes; log deployment in the commit trail**

---

### Task 8: Web — HVAC sub-rows in the breakdown

**Files:**
- Modify: `web/lib/influx.ts`, `web/lib/energyWindow.ts`, `web/components/ExplorerClient.tsx`
- Test: `web/lib/energyWindow.test.ts`

**Interfaces:**
- Consumes: the `hvac_mode` measurement (Task 7); existing `EnergyRow` type (`{category, kwh, parent?, prevKwh?, windowMs?}`), `mergeEnergyRows`, `BUCKET`/`ORG`/`makeClient`/`fluxDate` in `influx.ts`.
- Produces: `queryEnergyByCategory` category view returns Heating / Cooling / Hot Water rows with `parent: "HVAC"` spliced directly after the HVAC row; `BreakdownTable` renders them nested with zero changes (it already handles `parent` rows from the drill path).

- [ ] **Step 1: Write the failing vitest tests**

In `web/lib/energyWindow.test.ts` add:

```ts
import { spliceChildRows } from "./energyWindow";

describe("spliceChildRows", () => {
  const rows = [
    { category: "HVAC", kwh: 10 },
    { category: "Lights", kwh: 5 },
    { category: "Unmonitored", kwh: 2 },
    { category: "Heating", kwh: 6, parent: "HVAC" },
    { category: "Hot Water", kwh: 2, parent: "HVAC" },
  ];

  it("moves parent-tagged rows directly after their parent", () => {
    const out = spliceChildRows(rows, "HVAC");
    expect(out.map((r) => r.category)).toEqual([
      "HVAC", "Heating", "Hot Water", "Lights", "Unmonitored",
    ]);
  });

  it("drops children whose parent row is absent", () => {
    const out = spliceChildRows(rows.filter((r) => r.category !== "HVAC"), "HVAC");
    expect(out.every((r) => !r.parent)).toBe(true);
  });

  it("is a no-op when there are no children", () => {
    const plain = rows.filter((r) => !r.parent);
    expect(spliceChildRows(plain, "HVAC")).toEqual(plain);
  });
});

describe("hvacModeRowsFromFieldSums", () => {
  it("maps mode energy fields to nested display rows, dropping ~zero modes", () => {
    const out = hvacModeRowsFromFieldSums({
      energy_heat_kwh: 41.2,
      energy_cool_kwh: 0.001,
      energy_hot_water_kwh: 3.1,
    });
    expect(out).toEqual([
      { category: "Heating", kwh: 41.2, parent: "HVAC" },
      { category: "Hot Water", kwh: 3.1, parent: "HVAC" },
    ]);
  });

  it("returns [] for an empty window (pre-2026 or no data)", () => {
    expect(hvacModeRowsFromFieldSums({})).toEqual([]);
  });
});
```

(Import `hvacModeRowsFromFieldSums` from `./energyWindow` too.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npm test`
Expected: new tests FAIL (functions don't exist); everything else PASSES.

- [ ] **Step 3: Implement the pure helpers**

In `web/lib/energyWindow.ts`:

```ts
/** Display labels for the hvac_mode energy fields shown as HVAC sub-rows.
 *  idle/ambiguous are deliberately absent: they stay inside the HVAC parent's
 *  remainder rather than rendering as noise rows. */
const HVAC_MODE_LABELS: Record<string, string> = {
  energy_heat_kwh: "Heating",
  energy_cool_kwh: "Cooling",
  energy_hot_water_kwh: "Hot Water",
};

/** Threshold below which a mode row is noise, not information. */
const HVAC_MODE_MIN_KWH = 0.05;

/** Per-field kWh sums from the hvac_mode measurement → nested EnergyRows.
 *  Order is fixed (heat, cool, hot water) so the table is stable across
 *  windows regardless of magnitude. */
export function hvacModeRowsFromFieldSums(
  sums: Record<string, number>,
): EnergyRow[] {
  return Object.entries(HVAC_MODE_LABELS).flatMap(([field, label]) => {
    const kwh = sums[field] ?? 0;
    return kwh > HVAC_MODE_MIN_KWH ? [{ category: label, kwh, parent: "HVAC" }] : [];
  });
}

/** Move rows tagged `parent` to directly after their parent row, preserving
 *  their relative order. Children with no parent row present are dropped —
 *  same safety stance as mergeDrillRows. */
export function spliceChildRows(rows: EnergyRow[], parent: string): EnergyRow[] {
  const children = rows.filter((r) => r.parent === parent);
  const rest = rows.filter((r) => r.parent !== parent);
  if (children.length === 0) return rows;
  if (!rest.some((r) => r.category === parent && !r.parent)) return rest;
  return rest.flatMap((r) =>
    r.category === parent && !r.parent ? [r, ...children] : [r],
  );
}
```

Run `npm test` — the new energyWindow tests should pass now.

- [ ] **Step 4: Wire the Influx query**

In `web/lib/influx.ts`, next to `queryPanelKwh`:

```ts
/**
 * HVAC mode split (kWh per mode) over [fromMs, toMs), from the hvac_mode
 * timeline the Pi classifier writes (#14 sub-project 2). Returns nested
 * EnergyRows (parent: "HVAC"); empty for windows before the series starts
 * (2026-01-04) or if the classifier is down — the breakdown then simply
 * shows the plain HVAC row, nothing invented.
 */
async function queryHvacModeRows(fromMs: number, toMs: number): Promise<EnergyRow[]> {
  const flux = `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(fromMs)}, stop: ${fluxDate(toMs)})
  |> filter(fn: (r) => r._measurement == "hvac_mode")
  |> filter(fn: (r) => r._field == "energy_heat_kwh" or r._field == "energy_cool_kwh" or r._field == "energy_hot_water_kwh")
  |> group(columns: ["_field"])
  |> sum()
`;
  const sums: Record<string, number> = {};
  const queryApi = makeClient().getQueryApi(ORG);
  for await (const row of queryApi.iterateRows(flux)) {
    const o = row.tableMeta.toObject(row.values) as Record<string, unknown>;
    sums[String(o._field)] = Number(o._value) || 0;
  }
  return hvacModeRowsFromFieldSums(sums);
}
```

(Import `hvacModeRowsFromFieldSums` and `spliceChildRows` from `./energyWindow`.)

In `queryEnergyByCategory`, extend the concurrent fetch and splice into the return:

```ts
const [parts, panelKwh, modeRows] = await Promise.all([
  Promise.all(
    segments.map(async (seg) => { /* unchanged */ }),
  ),
  g.kind === "circuit" ? Promise.resolve(0) : queryPanelKwh(fromMs, toMs),
  g.kind === "circuit" ? Promise.resolve([]) : queryHvacModeRows(fromMs, toMs),
]);
```

and change the final return of the category path to:

```ts
return spliceChildRows(
  [...merged, { category: "Unmonitored", kwh: unmonitoredKwh(panelKwh, circuitKwh) }, ...modeRows],
  "HVAC",
);
```

No cache changes needed: the rows ride inside the existing `cachedQueryEnergyByCategory` entry, keyed by window as before. The Δ column works untouched — the previous window's response contains the same mode rows, and `buildEnergyRows` maps `prevKwh` by category name.

- [ ] **Step 5: Fix the show-filter to keep nested rows with their parent**

In `web/components/ExplorerClient.tsx`, the `filtered` computation currently drops any row whose `category` isn't in `view.show` — which would strip the mode rows whenever a filter is active. Change:

```ts
const filtered =
  view.show.length === 0
    ? rows
    : rows.filter(
        (r) =>
          view.show.includes(r.category) ||
          (r.parent !== undefined && view.show.includes(r.parent)),
      );
```

Note on drill interplay (#12): when the user drills HVAC into circuits, `mergeDrillRows` splices circuit rows immediately after the HVAC parent — ahead of the mode rows, which then follow. Both child sets render nested; they answer different questions (which wire vs. what for). Deliberate, leave it.

- [ ] **Step 6: Run the full web suite + typecheck**

Run: `cd web && npm test && npx tsc --noEmit`
Expected: all PASS, no type errors.

- [ ] **Step 7: Commit and push (Vercel auto-deploys from GitHub)**

```bash
git add web/lib/influx.ts web/lib/energyWindow.ts web/lib/energyWindow.test.ts web/components/ExplorerClient.tsx
git commit -m "web: split HVAC into Heating/Cooling/Hot Water sub-rows (#14 sub-project 2)"
git push
```

---

### Task 9: Live verification + docs

Main session; needs Nico for the smoke test.

**Files:**
- Modify: `CLAUDE.md` (Next Steps + architecture bullet)

- [ ] **Step 1: Smoke test (give Nico these exact self-contained steps)**

Open https://span.pianohouseproject.org — pick the 7d range. **Pass:** the HVAC row shows indented `└ Heating` / `└ Cooling` / `└ Hot Water` rows beneath it (whichever modes are nonzero this week); their kWh sum to at most the HVAC row's kWh; the Total row equals the sum of top-level rows only (sub-rows not double-counted); toggling a category filter that includes HVAC keeps the sub-rows, one that excludes HVAC hides them. **Fail:** sub-rows missing entirely, appearing at the bottom of the table un-nested, or Total jumping when they appear.

- [ ] **Step 2: Reconciliation sanity vs. reality**

August window: Cooling + Hot Water should dominate, Heating ≈ 0. Cross-check one week's Hot Water kWh against that week's `bath_event` energy sum — Hot Water must be ≥ the bath total (baths are a subset of DHW).

- [ ] **Step 3: Update CLAUDE.md**

- Architecture list: add `hvac_classifier.py` one-liner (mirroring `weather_poller.py`'s entry) and update `bath_detector.py`'s line (now timeline-based).
- Next Steps: mark #14 sub-project 2 shipped; note the natural follow-ups now unblocked (shower/laundry predicates as `attribution.py` one-liners, #3 cold-weather suppression, recirc-pump retro-analysis from overnight hot_water energy around 2026-04-09) — as pointers, not new scope.
- Note whether `hvac_classifier` inherited weather_poller's dead-service blind spot (it did — same "no health point" caveat; add it to that existing Next Steps bullet).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "CLAUDE.md: #14 sub-project 2 shipped — hvac_mode timeline + web split"
git push
```

---

## Self-review notes (already applied)

- Spec coverage: storage schema (Task 4, fields-not-tags per spec amendment), classifier + service (1/2/4), Phase 0 gates (5), bath re-base + parity (5's compare-baths + 6), backfill + conservation (7), web sub-rows + reconciliation (8/9), "delete not flag off" (6), Dockerfile COPY gotcha (4), machine placement (all Pi).
- Deliberately out of scope, per spec: weekly-email split, shower/laundry predicates, water-bill estimate, weather/classifier health points (recorded as a caveat in Task 9, not built).
- Type consistency: interval dict keys (`hp_mean_w`, `aux_mean_w`, …) are identical across `hvac_modes` output, `write_intervals` input, `query_timeline` output, and `attribution` input; `bath_events` emits the historical `bath_event` key names (`hp_mean_power_w`, …) — the two namings are different on purpose (timeline vs. event schema) and only `bath_events` translates between them.
