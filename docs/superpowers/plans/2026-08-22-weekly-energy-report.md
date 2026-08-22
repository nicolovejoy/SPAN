# Weekly Energy Report + Anomaly Email Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the nine-section daily email in `pi/daily_report.py` with a Monday weekly
briefing (headline, week-by-day chart, 12-week trend, merged usage table, HVAC block) plus a
daily anomaly email that sends only when a category deviates from its own baseline.

**Architecture:** Two files. `pi/daily_report.py` keeps its existing Influx client, `send_email`,
CSS, and the #9 raw/rollup segment-routing machinery (left in place, unused by the new report —
see "What stays, what goes" below), and gains a new query/render/orchestration layer that reads
**only** the `circuit_1h` rollup's `energy_wh_counter` field. `pi/report_baseline.py` is new: pure
median/MAD baseline math, weekday bucketing, and repeat-suppression state, with no Influx or email
code — `daily_report.py` owns all I/O and calls into it.

**Tech Stack:** Python 3.11, `influxdb-client`, `httpx` (Resend), `matplotlib` (charts), stdlib
`unittest`.

**Spec:** `docs/superpowers/specs/2026-08-21-weekly-energy-report-design.md` — read it before
starting; this plan argues from it and does not repeat its rationale.

## Global Constraints

- Energy field is **`energy_wh_counter`**, never `energy_wh` — the design's whole rationale is
  deltas immune to missed polls (spec "Data source").
- Data source is **`circuit_1h` only** — never raw `circuit`, never `circuit_5m`. No raw fallback
  for this report; the report always looks at data at least a day old, well past `circuit_1h`'s
  5–65 min tail lag (spec "Data source", `pi/influx_tasks/README.md` "Tail freshness").
- Day buckets are **Pacific-aligned**, via `option location = timezone.location(name:
  "America/Los_Angeles")` in Flux and `LOCAL_TZ` in Python — never UTC slicing (project
  convention in `CLAUDE.md`, spec "Data source").
- `circuit_1h` is **stop-stamped** (verified invariant, `pi/influx_tasks/README.md` "Timestamp
  convention") — hard-code this; do not reuse `_rollup_stamp()`'s runtime detection, which probes
  against raw data this report is explicitly designed not to touch.
- Cost model: `pi/rates.py` (`ENERGY_RATE = 0.1241`, `BASE_CHARGE_DAILY = 0.83`) is **already**
  flat SCL, matching `web/lib/rates.ts` — this was done 2026-05-15 (commit `935e185`). The spec's
  "converge the cost model" goal is already satisfied; reuse `pi/rates.py` as-is, no changes to it.
- Report weeks run **Monday–Sunday**.
- `#15` (`energy_wh_counter` as authoritative for `web/`) is explicitly **not** a dependency of
  this work — see spec "Data source" correction. Do not block on it or touch that worktree.

### What stays, what goes in `pi/daily_report.py`

The #9 segment router (`_run_segments`, `_circuit_segments`, `rollup_span`, `_rollup_stamp`,
`_circuit_kwh_flux`, `_circuit_records`, `_summed_rows`, `_merge_keyed`, `MEAS_RAW`/`MEAS_5M`/
`MEAS_1H`, `ROLLUP_PERIOD`/`ROLLUP_EVERY`) and its dedicated test file
(`pi/test_daily_report_rollups.py`) are **left untouched**. After this plan, nothing in
`daily_report.py` calls them — the new report bypasses that machinery entirely per the
circuit_1h-only constraint above. Removing a documented, independently-tested subsystem is a
separate decision from building this report; it is out of scope here and noted as a candidate
cleanup in Task 11.

Also kept, reused as-is: `flux_ts`, `local_day_utc_range`, `_shift_ts`, `_flux_dur`, `_local_day`,
`_delta_arrow`, `_fig_to_b64`, `_chart_img`, `cost_n_days`, `display_bucket`, `_load_bucket_rules`,
`_FALLBACK_RULES`, `add_months`, `query_daily_panel_kwh`, `send_email`, `CSS`,
`seconds_until_hour`.

Retired in Task 5 (superseded by the new weekly briefing; confirmed via
`grep -rn <symbol> pi/ web/` that nothing outside `daily_report.py` references any of them):
`AUX_HEAT_ALARM_USD`, `AUX_CIRCUIT_PATTERN`, `EV_CIRCUIT`, `ev_name_filter`,
`query_hourly_circuit_kwh`, `query_daily_circuit_kwh`, `query_monthly_circuit_kwh`,
`query_circuit_kwh_by_name`, `query_circuit_energy`, `query_interval_panel_kwh`,
`query_interval_circuit_kwh`, `query_interval_circuit_kwh_summed`, `query_monthly_panel_kwh`,
`latest_complete_month`, `build_today_series`, `build_week_series`, `render_today_chart`,
`render_week_compare`, `render_monthly_chart`, `build_monthly_section`, `merge_circuits`,
`_aggregate_by_bucket`, `event_summary`, `query_events`, `_event_time`, `Period`, `ReportContext`,
`build_context`, `generate_report`, every `section_*` function, `SECTIONS`, the old `build_html`,
the old `main()` body, `CATEGORY_COLORS` (rebuilt fresh against the new chart layer in Task 5),
`_FALLBACK_CYCLE` (same).

---

## Phase 1 — Weekly briefing, blocks 1–4, on-demand test send

### Task 1: `circuit_1h` counter query layer

**Files:**
- Modify: `pi/daily_report.py` (add new section near the existing `# ---------- circuit source
  routing (issue #9) ----------` block — put the new functions in a clearly separated section
  below it, e.g. after line 358 where `_run_segments` ends)
- Test: `pi/test_weekly_report.py` (new)

**Interfaces:**
- Consumes: `ROLLUP_PERIOD[MEAS_1H]` (`timedelta(hours=1)`), `MEAS_1H` (`"circuit_1h"`),
  `_shift_ts`, `_flux_dur`, `_local_day`, `LOCAL_TZ_NAME`, `INFLUXDB_BUCKET`, `INFLUXDB_ORG` — all
  already defined earlier in `pi/daily_report.py`.
- Produces: `_counter_kwh_flux(start, stop, every, name_filter=None) -> str` and
  `query_daily_circuit_counter_kwh(query_api, start, stop) -> list[tuple[str, date, float]]`
  (`(circuit_name, local_date, kwh)`), used by every later task that needs circuit energy.

- [ ] **Step 1: Write the failing tests**

```python
# pi/test_weekly_report.py
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: `AttributeError: module 'daily_report' has no attribute '_counter_kwh_flux'`

- [ ] **Step 3: Implement**

Add after `_run_segments` (around line 358 in the current file):

```python
# ---------- weekly report + anomaly check: energy_wh_counter query layer ----------
#
# Deliberately narrower than the #9 segment router above: the weekly briefing and
# the daily anomaly check both read circuit_1h.energy_wh_counter only, never raw
# and never circuit_5m (design doc "Data source"). Every window this code asks
# for ends at least a day in the past, well past circuit_1h's 5-65 min tail lag,
# so there is no fresh-tail case to handle and no raw fallback is needed.
# circuit_1h is stop-stamped (verified invariant, influx_tasks/README.md) — that
# is hard-coded below rather than detected at runtime.

COUNTER_FIELD = "energy_wh_counter"


def _counter_kwh_flux(start: str, stop: str, every: str,
                      name_filter: str | None = None) -> str:
    """circuit_1h.energy_wh_counter summed into `every`-windows, in kWh.
    Pacific-aligned for 1d/1mo grids. Re-centred from its stop stamp to the
    bucket midpoint (shift range forward one period, then timeShift back half a
    period) so a bucket can never land in the neighbouring output window —
    the same recentring _circuit_kwh_flux does for stamp="stop"."""
    period = ROLLUP_PERIOD[MEAS_1H]
    mid = -period / 2
    rng_start, rng_stop = _shift_ts(start, period), _shift_ts(stop, period)
    nf = f'  |> filter(fn: (r) => r.name =~ /(?i){name_filter}/)\n' if name_filter else ''
    loc = (f'import "timezone"\noption location = timezone.location(name: "{LOCAL_TZ_NAME}")\n\n'
           if every in ("1d", "1mo") else '')
    return f'''{loc}from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {rng_start}, stop: {rng_stop})
  |> filter(fn: (r) => r._measurement == "{MEAS_1H}" and r._field == "{COUNTER_FIELD}")
  |> filter(fn: (r) => exists r._value)
{nf}  |> timeShift(duration: {_flux_dur(mid)})
  |> aggregateWindow(every: {every}, fn: sum, createEmpty: false)
  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
'''


def query_daily_circuit_counter_kwh(query_api, start: str, stop: str) -> list[tuple[str, date, float]]:
    """(circuit_name, local_date, kwh) via circuit_1h.energy_wh_counter — one row
    per circuit per Pacific day covered by [start, stop). The single workhorse
    query for the weekly briefing and the anomaly check: everything else (week
    totals, month totals, category rollups) is derived from these rows in pure
    Python — see the grouping helpers below."""
    flux = _counter_kwh_flux(start, stop, "1d")
    out: list[tuple[str, date, float]] = []
    for table in query_api.query(flux, org=INFLUXDB_ORG):
        for rec in table.records:
            out.append((rec.values.get("name", "Unknown"), _local_day(rec.get_time()),
                       rec.get_value() or 0.0))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: all `CounterFluxTest` and `QueryDailyCounterTest` cases PASS. Also re-run
`python3 test_daily_report_rollups.py -v` to confirm the #9 tests are untouched.

- [ ] **Step 5: Commit**

```bash
git add pi/daily_report.py pi/test_weekly_report.py
git commit -m "feat: add circuit_1h energy_wh_counter query layer for the weekly report"
```

---

### Task 2: Pure date/grouping helpers

**Files:**
- Modify: `pi/daily_report.py` (add below Task 1's new section)
- Test: `pi/test_weekly_report.py`

**Interfaces:**
- Consumes: `query_daily_circuit_counter_kwh`'s row shape `(name, date, kwh)`; `display_bucket`,
  `_load_bucket_rules`, `_FALLBACK_RULES` (existing); `ENERGY_RATE` from `rates` (existing import).
- Produces: `local_week_start`, `category_day_kwh`, `week_totals`, `circuit_week_totals`,
  `category_top_circuits`, `trailing_week_starts`, `_sum_days`, `unmonitored_week_kwh`,
  `_all_categories` — all pure, all consumed by Task 4's `build_weekly_context`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: `AttributeError: module 'daily_report' has no attribute 'local_week_start'` (and similar
for each new name, as `unittest` reaches them).

- [ ] **Step 3: Implement**

```python
def local_week_start(d: date) -> date:
    """Monday on or before `d` — report weeks run Monday-Sunday."""
    return d - timedelta(days=d.weekday())


def category_day_kwh(rows: list[tuple[str, date, float]]) -> dict[date, dict[str, float]]:
    """rows -> {local_date: {category: kwh}}, circuits rolled up via display_bucket."""
    out: dict[date, dict[str, float]] = {}
    for name, day, kwh in rows:
        cat = display_bucket(name)
        day_map = out.setdefault(day, {})
        day_map[cat] = day_map.get(cat, 0.0) + kwh
    return out


def week_totals(day_cat: dict[date, dict[str, float]], week_start: date) -> dict[str, float]:
    """Sum category kWh over [week_start, week_start+7)."""
    week_end = week_start + timedelta(days=7)
    out: dict[str, float] = {}
    for day, cats in day_cat.items():
        if week_start <= day < week_end:
            for cat, kwh in cats.items():
                out[cat] = out.get(cat, 0.0) + kwh
    return out


def circuit_week_totals(rows: list[tuple[str, date, float]], week_start: date) -> dict[str, float]:
    """Per-circuit (not per-category) kWh over [week_start, week_start+7)."""
    week_end = week_start + timedelta(days=7)
    out: dict[str, float] = {}
    for name, day, kwh in rows:
        if week_start <= day < week_end:
            out[name] = out.get(name, 0.0) + kwh
    return out


def category_top_circuits(rows: list[tuple[str, date, float]], week_start: date,
                          category: str, n: int = 5) -> list[tuple[str, float]]:
    """Top-n circuits by kWh within `category`, for the usage table's nested rows."""
    totals = circuit_week_totals(rows, week_start)
    names = [name for name in totals if display_bucket(name) == category]
    return sorted(((name, totals[name]) for name in names), key=lambda x: -x[1])[:n]


def trailing_week_starts(target_week_start: date, n: int) -> list[date]:
    """The n Mondays strictly before target_week_start, oldest first."""
    return [target_week_start - timedelta(days=7 * i) for i in range(n, 0, -1)]


def _sum_days(daily: dict[date, float], lo: date, hi_exclusive: date) -> float:
    return sum(v for d, v in daily.items() if lo <= d < hi_exclusive)


def unmonitored_week_kwh(panel_week_kwh: float, circuit_totals: dict[str, float]) -> float:
    """Panel total minus every known circuit — the energy the panel meters but no
    circuit sensor does (no washer/dryer/water-heater circuit; see #17). Floored
    at zero: circuit-level counter noise can occasionally exceed a noisy panel
    integral over a short window, and a negative "unmonitored" number is never
    meaningful."""
    return max(0.0, panel_week_kwh - sum(circuit_totals.values()))


def _all_categories() -> list[str]:
    """Category display order: categories.json rules, then its default bucket."""
    rules, default = _load_bucket_rules()
    return [c for c, _ in rules] + [default]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pi/daily_report.py pi/test_weekly_report.py
git commit -m "feat: add pure week/category grouping helpers for the weekly report"
```

---

### Task 3: Headline computation

**Files:**
- Modify: `pi/daily_report.py`
- Test: `pi/test_weekly_report.py`

**Interfaces:**
- Consumes: `cost_n_days` (existing, from `pi/rates.py` via `ENERGY_RATE`/`BASE_CHARGE_DAILY`).
- Produces: `_pct_delta(current, baseline) -> float | None`,
  `headline_stats(week_kwh, last_week_kwh, trailing12_avg_kwh, week_cat, last_week_cat) -> dict`
  with keys `kwh`, `cost`, `delta_vs_last_week_pct`, `delta_vs_12wk_pct`, `top_mover`,
  `top_mover_delta_kwh` — consumed by Task 4/5.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: `AttributeError: module 'daily_report' has no attribute '_pct_delta'`

- [ ] **Step 3: Implement**

```python
def _pct_delta(current: float, baseline: float) -> float | None:
    return None if baseline <= 0 else (current - baseline) / baseline * 100.0


def headline_stats(week_kwh: float, last_week_kwh: float, trailing12_avg_kwh: float,
                   week_cat: dict[str, float], last_week_cat: dict[str, float]) -> dict:
    """Block 1's numbers. Largest mover excludes "Unmonitored" — it's a metering
    accounting row, not a category a reader can act on."""
    movers = {c: week_cat.get(c, 0.0) - last_week_cat.get(c, 0.0)
             for c in (set(week_cat) | set(last_week_cat)) - {"Unmonitored"}}
    top_mover = max(movers, key=lambda c: abs(movers[c])) if movers else None
    return {
        "kwh": week_kwh,
        "cost": cost_n_days(week_kwh, 7),
        "delta_vs_last_week_pct": _pct_delta(week_kwh, last_week_kwh),
        "delta_vs_12wk_pct": _pct_delta(week_kwh, trailing12_avg_kwh),
        "top_mover": top_mover,
        "top_mover_delta_kwh": movers.get(top_mover, 0.0) if top_mover else 0.0,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pi/daily_report.py pi/test_weekly_report.py
git commit -m "feat: add weekly headline stats computation"
```

---

### Task 4: `WeeklyContext` + `build_weekly_context` orchestration

**Files:**
- Modify: `pi/daily_report.py`
- Test: `pi/test_weekly_report.py`

**Interfaces:**
- Consumes: `query_daily_circuit_counter_kwh`, `query_daily_panel_kwh` (existing), `local_week_start`,
  `category_day_kwh`, `week_totals`, `circuit_week_totals`, `category_top_circuits`,
  `trailing_week_starts`, `_sum_days`, `unmonitored_week_kwh`, `_all_categories`, `headline_stats`,
  `ENERGY_RATE` (from `rates`).
- Produces: `WeeklyContext` dataclass (fields: `week_start`, `rows`, `panel_daily`, `day_cat`,
  `categories`, `headline`, `usage_rows`, `week_by_day`, `trend`, and a `date_str` property) and
  `build_weekly_context(query_api, week_start) -> WeeklyContext` — consumed by every render in
  Task 5.

- [ ] **Step 1: Write the failing test**

```python
from unittest import mock


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: `AttributeError: module 'daily_report' has no attribute 'build_weekly_context'`

- [ ] **Step 3: Implement**

```python
@dataclass
class WeeklyContext:
    """Everything the weekly-briefing renders need. `week_start` is the target
    week's Monday; `rows`/`panel_daily` span the full 98-day fetch window so
    the 12-week trend and HVAC's month-over-month (Task 7) can be derived
    without a second Influx round trip."""
    week_start: date
    rows: list[tuple[str, date, float]]
    panel_daily: dict[date, float]
    day_cat: dict[date, dict[str, float]]
    categories: list[str]
    headline: dict
    usage_rows: list[dict]
    week_by_day: list[tuple[date, dict[str, float]]]
    trend: list[tuple[date, dict[str, float]]]

    @property
    def date_str(self) -> str:
        week_end = self.week_start + timedelta(days=6)
        return f'{self.week_start.strftime("%b %-d")}–{week_end.strftime("%b %-d, %Y")}'


def build_weekly_context(query_api, week_start: date) -> WeeklyContext:
    """Window conventions:
      TARGET WEEK = [week_start, week_start+7)               Monday-Sunday
      FETCH       = [week_start-98, week_start+7)             14 weeks back,
                    covering the 12-week trend (block 3) and a full 2-month
                    look-back for HVAC month-over-month (Task 7)
    """
    fetch_start_date = week_start - timedelta(days=98)
    fetch_start = flux_ts(local_day_utc_range(fetch_start_date)[0])
    fetch_stop = flux_ts(local_day_utc_range(week_start + timedelta(days=7))[0])

    rows = query_daily_circuit_counter_kwh(query_api, fetch_start, fetch_stop)
    panel_daily = dict(query_daily_panel_kwh(query_api, fetch_start, fetch_stop))
    day_cat = category_day_kwh(rows)
    categories = _all_categories()

    last_week_start = week_start - timedelta(days=7)
    this_week_cat = week_totals(day_cat, week_start)
    last_week_cat = week_totals(day_cat, last_week_start)
    trailing12_starts = trailing_week_starts(week_start, 12)

    week_panel_kwh = _sum_days(panel_daily, week_start, week_start + timedelta(days=7))
    last_week_panel_kwh = _sum_days(panel_daily, last_week_start, week_start)
    trailing12_panel = [_sum_days(panel_daily, ws, ws + timedelta(days=7))
                        for ws in trailing12_starts]
    trailing12_avg_panel = sum(trailing12_panel) / len(trailing12_panel) if trailing12_panel else 0.0

    headline = headline_stats(week_panel_kwh, last_week_panel_kwh, trailing12_avg_panel,
                              this_week_cat, last_week_cat)

    circuit_totals = circuit_week_totals(rows, week_start)
    usage_rows = []
    for cat in categories:
        kwh = this_week_cat.get(cat, 0.0)
        wk12 = [week_totals(day_cat, ws).get(cat, 0.0) for ws in trailing12_starts]
        avg12 = sum(wk12) / len(wk12) if wk12 else 0.0
        usage_rows.append({
            "category": cat,
            "kwh": kwh,
            "cost": round(kwh * ENERGY_RATE, 2),
            "delta_week_pct": _pct_delta(kwh, last_week_cat.get(cat, 0.0)),
            "delta_12wk_pct": _pct_delta(kwh, avg12),
            "top_circuits": category_top_circuits(rows, week_start, cat),
        })

    unmon = unmonitored_week_kwh(week_panel_kwh, circuit_totals)
    last_unmon = unmonitored_week_kwh(last_week_panel_kwh, circuit_week_totals(rows, last_week_start))
    unmon_wk12 = [unmonitored_week_kwh(_sum_days(panel_daily, ws, ws + timedelta(days=7)),
                                       circuit_week_totals(rows, ws))
                 for ws in trailing12_starts]
    unmon_avg12 = sum(unmon_wk12) / len(unmon_wk12) if unmon_wk12 else 0.0
    usage_rows.append({
        "category": "Unmonitored",
        "kwh": unmon,
        "cost": round(unmon * ENERGY_RATE, 2),
        "delta_week_pct": _pct_delta(unmon, last_unmon),
        "delta_12wk_pct": _pct_delta(unmon, unmon_avg12),
        "top_circuits": [],
    })

    week_by_day = [
        (week_start + timedelta(days=i),
         {c: day_cat.get(week_start + timedelta(days=i), {}).get(c, 0.0) for c in categories})
        for i in range(7)
    ]
    trend = [(ws, week_totals(day_cat, ws)) for ws in trailing12_starts + [week_start]]

    return WeeklyContext(
        week_start=week_start, rows=rows, panel_daily=panel_daily, day_cat=day_cat,
        categories=categories, headline=headline, usage_rows=usage_rows,
        week_by_day=week_by_day, trend=trend,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pi/daily_report.py pi/test_weekly_report.py
git commit -m "feat: add WeeklyContext orchestration for the weekly report"
```

---

### Task 5: Block renders, wiring, retire old code

**Files:**
- Modify: `pi/daily_report.py` (replace `CATEGORY_COLORS`/`_FALLBACK_CYCLE` through the old
  `main()` — i.e. everything from the old chart-rendering section, line ~585, through EOF — with
  the content below; delete every symbol listed in "What stays, what goes" above)
- Test: `pi/test_weekly_report.py`

**Interfaces:**
- Consumes: `WeeklyContext` (Task 4), `_delta_arrow`, `_fig_to_b64`, `_chart_img`, `CSS`,
  `send_email` (all existing, kept).
- Produces: `render_headline`, `render_week_by_day_chart`, `render_12wk_trend_chart`,
  `render_usage_table`, `WEEKLY_SECTIONS`, `build_weekly_html`, `generate_weekly_report`. Task 7
  (HVAC block) appends one more render to `WEEKLY_SECTIONS`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: `AttributeError: module 'daily_report' has no attribute 'render_headline'`

- [ ] **Step 3: Implement**

First, delete every symbol listed under "Retired in Task 5" in Global Constraints. Then add:

```python
CATEGORY_COLORS = {
    "Lights": "#f1c40f", "HVAC": "#e74c3c", "Car": "#3498db",
    "Appliances": "#e67e22", "Else": "#16a085", "Unmonitored": "#95a5a6",
}


def render_headline(ctx: WeeklyContext) -> str:
    h = ctx.headline
    week_delta = _delta_arrow_pct(h["delta_vs_last_week_pct"], " vs last week")
    avg_delta = _delta_arrow_pct(h["delta_vs_12wk_pct"], " vs 12-wk avg")
    mover = (f' The biggest mover was <strong>{h["top_mover"]}</strong> '
            f'({h["top_mover_delta_kwh"]:+.1f} kWh vs last week).'
            if h["top_mover"] else "")
    return f'''<h2>Weekly Energy Report &mdash; {ctx.date_str}</h2>
<p style="font-size:15px;">
<strong>{h["kwh"]:.1f} kWh</strong> (${h["cost"]:.2f}){week_delta}{avg_delta}.{mover}
</p>'''


def _delta_arrow_pct(pct: float | None, suffix: str) -> str:
    if pct is None:
        return ""
    arrow, color = ("&uarr;", "#e74c3c") if pct > 0 else ("&darr;", "#27ae60")
    return (f' <span style="color:{color};font-size:13px;font-weight:500;">'
           f'{arrow}{abs(pct):.0f}%{suffix}</span>')


def render_week_by_day_chart(ctx: WeeklyContext) -> str:
    """Block 2 — 7 bars stacked by category."""
    if not ctx.week_by_day:
        return ""
    labels = [d.strftime("%a %-m/%-d") for d, _ in ctx.week_by_day]
    fig, ax = plt.subplots(figsize=(7, 3.4), dpi=120)
    bottom = [0.0] * len(labels)
    for cat in ctx.categories:
        vals = [cats.get(cat, 0.0) for _, cats in ctx.week_by_day]
        ax.bar(labels, vals, bottom=bottom, width=0.6,
              color=CATEGORY_COLORS.get(cat, "#888"), label=cat)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_ylabel("kWh")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="upper left", fontsize=8, ncol=3)
    fig.autofmt_xdate()
    fig.tight_layout()
    b64 = _fig_to_b64(fig)
    return f'<h3>This week by day</h3>\n{_chart_img(b64, "This week by day, by category")}'


def render_12wk_trend_chart(ctx: WeeklyContext) -> str:
    """Block 3 — stacked histogram of weekly totals by category, 13 weeks
    (12 trailing + the target week) so direction and composition read in one
    image."""
    if not ctx.trend:
        return ""
    labels = [ws.strftime("%-m/%-d") for ws, _ in ctx.trend]
    fig, ax = plt.subplots(figsize=(7, 3.2), dpi=120)
    bottom = [0.0] * len(labels)
    for cat in ctx.categories:
        vals = [totals.get(cat, 0.0) for _, totals in ctx.trend]
        ax.bar(labels, vals, bottom=bottom, width=0.7,
              color=CATEGORY_COLORS.get(cat, "#888"), label=cat)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_xticklabels(labels, fontsize=8, rotation=45, ha="right")
    ax.set_ylabel("kWh / week")
    ax.grid(True, alpha=0.3, axis="y")
    ax.legend(loc="upper left", fontsize=8, ncol=3)
    fig.tight_layout()
    b64 = _fig_to_b64(fig)
    return f'<h3>12-week trend</h3>\n{_chart_img(b64, "12-week trend by category")}'


def render_usage_table(ctx: WeeklyContext) -> str:
    """Block 4 — one table replacing the old cost-breakdown + top-circuits
    sections. Per-category cost is energy-only (kWh * ENERGY_RATE); the base
    service charge isn't attributable to a category and only appears in the
    headline's whole-week cost."""
    def pct_cell(pct: float | None) -> str:
        if pct is None:
            return "&mdash;"
        arrow, color = ("&uarr;", "#e74c3c") if pct > 0 else ("&darr;", "#27ae60")
        return f'<span style="color:{color};">{arrow}{abs(pct):.0f}%</span>'

    rows_html = []
    for r in ctx.usage_rows:
        rows_html.append(
            f'<tr><td>{r["category"]}</td><td>{r["kwh"]:.1f}</td><td>${r["cost"]:.2f}</td>'
            f'<td>{pct_cell(r["delta_week_pct"])}</td><td>{pct_cell(r["delta_12wk_pct"])}</td></tr>'
        )
        if r["top_circuits"]:
            nested = ", ".join(f'{name} ({kwh:.1f} kWh)' for name, kwh in r["top_circuits"])
            rows_html.append(
                f'<tr><td colspan="5" style="font-size:11px;color:#888;padding-left:24px;">'
                f'{nested}</td></tr>'
            )
    return f'''<h3>Usage by category</h3>
<table>
<tr><th>Category</th><th>kWh</th><th>Cost</th><th>vs last wk</th><th>vs 12-wk avg</th></tr>
{"".join(rows_html)}
</table>'''


WEEKLY_SECTIONS = [render_headline, render_week_by_day_chart, render_12wk_trend_chart,
                  render_usage_table]


def build_weekly_html(ctx: WeeklyContext) -> str:
    body = "\n\n".join(s for s in (section(ctx) for section in WEEKLY_SECTIONS) if s)
    return f'''<!DOCTYPE html>
<html><head><style>{CSS}</style></head>
<body>
{body}
</body></html>'''


def generate_weekly_report(client: InfluxDBClient, week_start: date):
    """Build and send the Monday briefing for the week starting `week_start`."""
    ctx = build_weekly_context(client.query_api(), week_start)
    send_email(build_weekly_html(ctx), f"Weekly Energy Report — {ctx.date_str}")


def main():
    parser = argparse.ArgumentParser(description="Weekly energy report + daily anomaly check")
    parser.add_argument("--loop", action="store_true",
                       help="Run forever: anomaly check daily, weekly briefing Mondays, "
                            "both at REPORT_HOUR local")
    parser.add_argument("--date", type=str,
                       help="Send the weekly briefing for the week containing this date "
                            "(YYYY-MM-DD) — on-demand test send")
    args = parser.parse_args()

    for var, name in [(INFLUXDB_TOKEN, "INFLUXDB_TOKEN"), (RESEND_API_KEY, "RESEND_API_KEY"),
                      (REPORT_EMAIL, "REPORT_EMAIL")]:
        if not var:
            logger.error(f"{name} not set")
            return

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
        logger.info(f"Generating weekly briefing for the week containing {args.date}")
        generate_weekly_report(client, local_week_start(target))
    elif args.loop:
        logger.info(f"Loop mode: weekly briefing Mondays at {REPORT_HOUR}:00")
        while True:
            wait = seconds_until_hour(REPORT_HOUR)
            logger.info(f"Next run in {wait / 3600:.1f} hours")
            time.sleep(wait)
            yesterday = (datetime.now() - timedelta(days=1)).date()
            if datetime.now().weekday() == 0:   # Monday: yesterday closed last week
                try:
                    generate_weekly_report(client, local_week_start(yesterday))
                except Exception as e:
                    logger.error(f"Weekly report failed: {e}")
    else:
        generate_weekly_report(client, local_week_start(datetime.now().date() - timedelta(days=7)))

    client.close()


if __name__ == "__main__":
    main()
```

Note: `main()` above is the Phase 1 version — Task 10 (Phase 3) extends it with the daily
anomaly-check path. Don't treat this as final; Task 10 replaces this function body again.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_weekly_report.py -v` and `cd pi && python3 test_daily_report_rollups.py -v`
Expected: both PASS. Also run `python3 -c "import daily_report"` from `pi/` to catch any leftover
reference to a retired symbol (a `NameError`/`AttributeError` at import time means something was
missed).

- [ ] **Step 5: Manual verification**

**Send a real test email:**

```bash
cd pi && docker compose run --rm daily-report python daily_report.py --date 2026-08-17
```

Check: an email arrives at the `REPORT_EMAIL` address with a headline line, a 7-bar stacked chart,
a 12-week (13-bar) stacked chart, and a usage table with 6 rows (5 categories + Unmonitored) each
showing nested top circuits. Compare the Unmonitored row's kWh against
`ctx.usage_rows` total vs a manual panel-total check if anything looks off.

- [ ] **Step 6: Commit**

```bash
git add pi/daily_report.py pi/test_weekly_report.py
git commit -m "feat: wire the weekly briefing (blocks 1-4) and retire the old daily sections"
```

---

### Task 6: Docker/docs updates for Phase 1

**Files:**
- Modify: `pi/CLAUDE.md` — none exists; update `/Users/nico/src/SPAN/CLAUDE.md` "Next Steps"
  entry for the weekly report instead (mark Phase 1 shipped)
- Modify: `pi/.env.example` — no changes needed (checked: `AUX_HEAT_ALARM_USD` was never
  documented there, nothing else references it)

**Interfaces:** none (docs only).

- [ ] **Step 1: Update CLAUDE.md**

In `/Users/nico/src/SPAN/CLAUDE.md`, under "Next Steps", replace the "Weekly energy report +
anomaly email" bullet's status line to note Phase 1 is live and link this plan:

```markdown
- **Weekly energy report** — Phase 1 shipped (headline, week-by-day chart, 12-week trend, usage
  table). Plan: `docs/superpowers/plans/2026-08-22-weekly-energy-report.md`. Remaining: HVAC block
  (Phase 2), anomaly email (Phase 3).
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: weekly report Phase 1 shipped"
```

---

## Phase 2 — HVAC block

### Task 7: HVAC by-day / week-over-week / month-over-month

**Files:**
- Modify: `pi/daily_report.py`
- Test: `pi/test_weekly_report.py`

**Interfaces:**
- Consumes: `WeeklyContext.day_cat`, `add_months` (existing, kept per Global Constraints),
  `calendar` (already imported).
- Produces: `mom_comparison(day_cat, as_of, category) -> tuple[float, float]`,
  `render_hvac_block(ctx) -> str`, appended to `WEEKLY_SECTIONS`.

- [ ] **Step 1: Write the failing tests**

```python
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
        day_cat = {date(2026, 2, 28): {"HVAC": 5.0}}
        # Jan has 31 days; as_of.day=31 should clamp to Feb 28
        this_month, last_month = dr.mom_comparison(day_cat, date(2026, 1, 31), "HVAC")
        self.assertAlmostEqual(this_month, 0.0)

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
```

(Remove the stray `test_render_hvac_block_includes_wow_and_mom` placeholder line above before
committing — it was scaffolding while drafting `make_ctx`; the smoke test below it is the real
assertion. This is a reminder for whoever implements this task, not a step to skip.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: `AttributeError: module 'daily_report' has no attribute 'mom_comparison'`

- [ ] **Step 3: Implement**

```python
def mom_comparison(day_cat: dict[date, dict[str, float]], as_of: date,
                   category: str) -> tuple[float, float]:
    """(this-month-to-date, same-cutoff last month) kWh for `category` — both
    covering day 1 through as_of.day of their respective months, so a
    16-days-in month compares fairly against a full 30-day one instead of
    against a partial vs. complete mismatch."""
    this_start = as_of.replace(day=1)

    def total(lo: date, hi: date) -> float:
        return sum(cats.get(category, 0.0) for d, cats in day_cat.items() if lo <= d <= hi)

    this_month = total(this_start, as_of)

    prev_y, prev_m = add_months(as_of.year, as_of.month, -1)
    prev_start = date(prev_y, prev_m, 1)
    prev_cutoff = min(as_of.day, calendar.monthrange(prev_y, prev_m)[1])
    prev_end = date(prev_y, prev_m, prev_cutoff)
    last_month = total(prev_start, prev_end)

    return this_month, last_month


def render_hvac_block(ctx: WeeklyContext) -> str:
    """Block 5 — HVAC by day (this week), week-over-week, month-over-month.
    Renders without a hot-water/space-conditioning split; gains a row when
    that detector work (out of scope here) lands."""
    hvac_by_day = [cats.get("HVAC", 0.0) for _, cats in ctx.week_by_day]
    if not any(hvac_by_day):
        return ""
    labels = [d.strftime("%a") for d, _ in ctx.week_by_day]
    fig, ax = plt.subplots(figsize=(7, 2.8), dpi=120)
    ax.bar(labels, hvac_by_day, width=0.6, color=CATEGORY_COLORS["HVAC"])
    ax.set_ylabel("kWh")
    ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout()
    b64 = _fig_to_b64(fig)

    week_end = ctx.week_start + timedelta(days=6)
    this_week = sum(hvac_by_day)
    last_week = week_totals(ctx.day_cat, ctx.week_start - timedelta(days=7)).get("HVAC", 0.0)
    this_month, last_month = mom_comparison(ctx.day_cat, week_end, "HVAC")

    wow = _delta_arrow_pct(_pct_delta(this_week, last_week), " vs last week")
    mom = _delta_arrow_pct(_pct_delta(this_month, last_month), " vs last month")
    return f'''<h3>HVAC</h3>
{_chart_img(b64, "HVAC by day this week")}
<p style="font-size:13px;color:#444;">
{this_week:.1f} kWh this week{wow} &middot; {this_month:.1f} kWh this month{mom}
</p>'''


WEEKLY_SECTIONS.append(render_hvac_block)
```

Move the `WEEKLY_SECTIONS.append(render_hvac_block)` line to sit directly after
`WEEKLY_SECTIONS = [...]`'s definition in Task 5's code (i.e. edit that list literal in place to
include `render_hvac_block` as its fifth entry) rather than appending at import time from a
different part of the file — keeps section order declared in one place.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: PASS.

- [ ] **Step 5: Manual verification**

```bash
cd pi && docker compose run --rm daily-report python daily_report.py --date 2026-08-17
```

Check: the email now has an HVAC section with a 7-bar chart and a WoW/MoM line beneath the usage
table.

- [ ] **Step 6: Commit**

```bash
git add pi/daily_report.py pi/test_weekly_report.py
git commit -m "feat: add HVAC block (by-day, week-over-week, month-over-month)"
```

---

## Phase 3 — Anomaly email

### Task 8: `report_baseline.py` — median/MAD and the trigger

**Files:**
- Create: `pi/report_baseline.py`
- Test: `pi/test_report_baseline.py`

**Interfaces:**
- Consumes: nothing (pure, stdlib only).
- Produces: `median`, `mad`, `Baseline`, `compute_baseline`, `AnomalyResult`, `evaluate`,
  `trailing_same_weekday_dates` — consumed by Task 9 (guards) and Task 10 (wiring).

- [ ] **Step 1: Write the failing tests**

```python
# pi/test_report_baseline.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 test_report_baseline.py -v`
Expected: `ModuleNotFoundError: No module named 'report_baseline'`

- [ ] **Step 3: Implement**

```python
#!/usr/bin/env python3
"""Pure baseline math for the daily anomaly check: median/MAD, weekday
bucketing, and the anomaly trigger. No Influx, no email — daily_report.py owns
all I/O and calls into this module. See
docs/superpowers/specs/2026-08-21-weekly-energy-report-design.md ("The anomaly
email")."""

from dataclasses import dataclass
from datetime import date, timedelta


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def mad(values: list[float], m: float | None = None) -> float:
    """Median absolute deviation, scaled by 1.4826 so it's comparable to a
    standard deviation for a roughly-normal sample."""
    if m is None:
        m = median(values)
    return 1.4826 * median([abs(v - m) for v in values])


@dataclass
class Baseline:
    median: float
    mad: float   # already scaled by 1.4826
    n: int       # samples the baseline was computed from


def compute_baseline(samples: list[float]) -> Baseline:
    m = median(samples)
    return Baseline(median=m, mad=mad(samples, m), n=len(samples))


@dataclass
class AnomalyResult:
    is_anomalous: bool
    z: float | None   # None when mad == 0 and the percentage fallback fired instead
    pct: float        # |value - median| / median * 100, for the email copy


def evaluate(value: float, baseline: Baseline) -> AnomalyResult:
    """Both conditions must hold:  |z| > 3  and  |value - m| > max(0.20*m, 1.0)
    Degenerate fallback when mad == 0 (a perfectly regular category):
    |value - m| > max(0.50*m, 1.0)."""
    m = baseline.median
    diff = abs(value - m)
    pct = (diff / m * 100.0) if m > 0 else (0.0 if diff == 0 else float("inf"))
    if baseline.mad == 0:
        floor = max(0.50 * m, 1.0)
        return AnomalyResult(is_anomalous=diff > floor, z=None, pct=pct)
    z = (value - m) / baseline.mad
    floor = max(0.20 * m, 1.0)
    return AnomalyResult(is_anomalous=(abs(z) > 3 and diff > floor), z=z, pct=pct)


def trailing_same_weekday_dates(target: date, n: int = 8) -> list[date]:
    """The n dates before `target` sharing its weekday, oldest first — e.g. the
    trailing 8 Tuesdays before a Tuesday target. Plain calendar-week
    arithmetic (timedelta weeks), so it is unaffected by DST boundaries."""
    return [target - timedelta(weeks=i) for i in range(n, 0, -1)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_report_baseline.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pi/report_baseline.py pi/test_report_baseline.py
git commit -m "feat: add median/MAD baseline math and the anomaly trigger"
```

---

### Task 9: `report_baseline.py` — coverage guards and suppression state

**Files:**
- Modify: `pi/report_baseline.py`
- Test: `pi/test_report_baseline.py`

**Interfaces:**
- Consumes: nothing new (stdlib `json`, `pathlib.Path`).
- Produces: `day_coverage_ok`, `category_coverage_ok`, `SuppressionState`, `load_state`,
  `save_state`, `clear_state`, `should_alert` — consumed by Task 10.

- [ ] **Step 1: Write the failing tests**

```python
import json
import tempfile
from pathlib import Path


class CoverageTest(unittest.TestCase):
    def test_day_coverage_threshold(self):
        self.assertTrue(rb.day_coverage_ok(0.90))
        self.assertFalse(rb.day_coverage_ok(0.89))

    def test_category_coverage_threshold(self):
        self.assertTrue(rb.category_coverage_ok(6))
        self.assertFalse(rb.category_coverage_ok(5))


class SuppressionStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_no_prior_alert(self):
        state = rb.load_state(self.path, "HVAC")
        self.assertIsNone(state.last_alert_date)

    def test_save_then_load_round_trips(self):
        rb.save_state(self.path, "HVAC", date(2026, 8, 18), 4.2)
        state = rb.load_state(self.path, "HVAC")
        self.assertEqual(state.last_alert_date, date(2026, 8, 18))
        self.assertAlmostEqual(state.last_z, 4.2)

    def test_save_preserves_other_categories(self):
        rb.save_state(self.path, "HVAC", date(2026, 8, 18), 4.2)
        rb.save_state(self.path, "Lights", date(2026, 8, 19), 3.5)
        data = json.loads(self.path.read_text())
        self.assertIn("HVAC", data)
        self.assertIn("Lights", data)

    def test_clear_removes_the_category_only(self):
        rb.save_state(self.path, "HVAC", date(2026, 8, 18), 4.2)
        rb.save_state(self.path, "Lights", date(2026, 8, 19), 3.5)
        rb.clear_state(self.path, "HVAC")
        self.assertIsNone(rb.load_state(self.path, "HVAC").last_alert_date)
        self.assertIsNotNone(rb.load_state(self.path, "Lights").last_alert_date)

    def test_a_six_day_heat_wave_produces_one_alert_not_six(self):
        """The failure this whole design exists to avoid (spec, 'Guard: repeat
        suppression'). A flat, non-worsening anomaly stays suppressed for the
        whole episode — only worsening or a return to normal lifts it, so
        elapsed time alone never re-triggers."""
        baseline = rb.compute_baseline([10, 11, 10, 9, 10, 11, 10, 9])  # HVAC-ish
        hot_value = 40.0   # anomalous every day of the heat wave, same severity
        alerts_sent = 0
        day = date(2026, 7, 20)   # Monday
        for i in range(6):
            today = day + timedelta(days=i)
            result = rb.evaluate(hot_value, baseline)
            state = rb.load_state(self.path, "HVAC")
            if rb.should_alert(result, state):
                alerts_sent += 1
                rb.save_state(self.path, "HVAC", today, result.z)
        self.assertEqual(alerts_sent, 1)

    def test_state_clears_once_back_to_normal(self):
        rb.save_state(self.path, "HVAC", date(2026, 8, 18), 4.5)
        baseline = rb.compute_baseline([10, 11, 10, 9, 10, 11, 10, 9])
        normal_result = rb.evaluate(10.0, baseline)
        self.assertFalse(normal_result.is_anomalous)
        # caller's responsibility per should_alert's docstring: clear on normal
        rb.clear_state(self.path, "HVAC")
        state = rb.load_state(self.path, "HVAC")
        self.assertIsNone(state.last_alert_date)
        # a fresh anomaly right after clearing should alert immediately, not be
        # suppressed by the just-cleared episode
        result = rb.evaluate(40.0, baseline)
        self.assertTrue(rb.should_alert(result, state))

    def test_worsening_deviation_breaks_the_suppression_window(self):
        rb.save_state(self.path, "HVAC", date(2026, 8, 18), 4.0)
        state = rb.load_state(self.path, "HVAC")
        result = rb.AnomalyResult(is_anomalous=True, z=5.5, pct=80.0)   # +37.5% over 4.0
        self.assertTrue(rb.should_alert(result, state))   # still anomalous, but materially worse

    def test_mild_worsening_stays_suppressed(self):
        rb.save_state(self.path, "HVAC", date(2026, 8, 18), 4.0)
        state = rb.load_state(self.path, "HVAC")
        result = rb.AnomalyResult(is_anomalous=True, z=4.5, pct=70.0)   # only +12.5%
        self.assertFalse(rb.should_alert(result, state))


class CoverageGapTest(unittest.TestCase):
    def test_a_coverage_gap_produces_zero_alerts(self):
        """spec, 'Guard: coverage check' — a collector outage must not be
        reported as an anomaly in every category."""
        self.assertFalse(rb.day_coverage_ok(0.40))  # caller suppresses the whole day
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 test_report_baseline.py -v`
Expected: `AttributeError: module 'report_baseline' has no attribute 'day_coverage_ok'`

- [ ] **Step 3: Implement**

Append to `pi/report_baseline.py`:

```python
def day_coverage_ok(fraction_present: float, threshold: float = 0.90) -> bool:
    """≥90% of a day's expected circuit_1h hours must be present, or the
    caller suppresses all alerting for that day (spec: coverage guard)."""
    return fraction_present >= threshold


def category_coverage_ok(samples_present: int, required: int = 6) -> bool:
    """≥6 of the 8 trailing same-weekday samples must be present, or the
    caller skips that category for the day."""
    return samples_present >= required


@dataclass
class SuppressionState:
    last_alert_date: date | None
    last_z: float | None


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_state(path: Path, category: str) -> SuppressionState:
    entry = _load_json(path).get(category)
    if not entry:
        return SuppressionState(None, None)
    return SuppressionState(
        last_alert_date=date.fromisoformat(entry["date"]) if entry.get("date") else None,
        last_z=entry.get("z"),
    )


def save_state(path: Path, category: str, alert_date: date, z: float | None) -> None:
    data = _load_json(path)
    data[category] = {"date": alert_date.isoformat(), "z": z}
    path.write_text(json.dumps(data))


def clear_state(path: Path, category: str) -> None:
    data = _load_json(path)
    if category in data:
        del data[category]
        path.write_text(json.dumps(data))


def should_alert(result: AnomalyResult, state: SuppressionState,
                 worsen_pct: float = 0.25) -> bool:
    """Repeat-suppression guard. Fires when the category is newly anomalous, or
    when a currently-suppressed anomaly has materially worsened (|z| grows by
    more than worsen_pct) since the last alert. A continuous anomaly of
    constant severity — a heat wave — alerts exactly once and then stays
    suppressed indefinitely, by design: nothing here re-triggers on elapsed
    time alone. The caller is responsible for calling clear_state() once a
    category returns to normal, which is what lets a *later, separate*
    anomalous episode alert immediately rather than staying suppressed by a
    stale prior episode."""
    if not result.is_anomalous:
        return False
    if state.last_alert_date is None:
        return True
    if state.last_z is not None and result.z is not None:
        return abs(result.z) > abs(state.last_z) * (1 + worsen_pct)
    return False
```

Add `import json` and `from pathlib import Path` to the top of `pi/report_baseline.py` alongside
the existing `dataclass`/`date`/`timedelta` imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_report_baseline.py -v`
Expected: PASS, including `test_a_six_day_heat_wave_produces_one_alert_not_six`.

- [ ] **Step 5: Commit**

```bash
git add pi/report_baseline.py pi/test_report_baseline.py
git commit -m "feat: add coverage guards and repeat-suppression state to report_baseline"
```

---

### Task 10: Wire the anomaly check into `daily_report.py`

**Files:**
- Modify: `pi/daily_report.py` (add coverage query + `generate_anomaly_check` + email render;
  replace `main()` from Task 5 with the version below)
- Test: `pi/test_weekly_report.py`

**Interfaces:**
- Consumes: everything from `report_baseline` (Tasks 8–9), `query_daily_circuit_counter_kwh`,
  `category_day_kwh`, `_all_categories`, `MEAS_1H`, `COUNTER_FIELD` (all from Task 1/2).
- Produces: `query_day_coverage`, `render_anomaly_email`, `generate_anomaly_check`, the final
  `main()`.

- [ ] **Step 1: Write the failing tests**

```python
class AnomalyCheckTest(unittest.TestCase):
    def test_query_day_coverage_flux_shape(self):
        flux = dr._day_coverage_flux(date(2026, 8, 17))
        self.assertIn('r._measurement == "circuit_1h" and r._field == "energy_wh_counter"', flux)
        self.assertIn("distinct(column: \"_time\")", flux)

    def test_render_anomaly_email_subject_names_category_and_direction(self):
        import report_baseline as rb
        baseline = rb.compute_baseline([10, 11, 10, 9, 10, 11, 10, 9])
        result = rb.evaluate(20.0, baseline)
        subject, html = dr.render_anomaly_email(
            date(2026, 8, 18),  # Tuesday
            [("HVAC", 20.0, baseline, result)],
            {"HVAC": [("Heat pump", 18.0)]},
        )
        self.assertIn("HVAC", subject)
        self.assertIn("above normal for a Tuesday", subject)
        self.assertIn("Heat pump", html)

    def test_generate_anomaly_check_sends_nothing_on_a_normal_day(self):
        with mock.patch.object(dr, "query_day_coverage", return_value=1.0), \
             mock.patch.object(dr, "query_daily_circuit_counter_kwh",
                              return_value=[("Heat pump", d, 10.0)
                                           for d in [date(2026, 8, 18) - timedelta(weeks=w)
                                                    for w in range(9)]]), \
             mock.patch.object(dr, "send_email") as sent:
            dr.generate_anomaly_check(mock.Mock(), date(2026, 8, 18))
            sent.assert_not_called()

    def test_generate_anomaly_check_suppresses_on_low_coverage(self):
        with mock.patch.object(dr, "query_day_coverage", return_value=0.4), \
             mock.patch.object(dr, "query_daily_circuit_counter_kwh") as q, \
             mock.patch.object(dr, "send_email") as sent:
            dr.generate_anomaly_check(mock.Mock(), date(2026, 8, 18))
            q.assert_not_called()   # short-circuits before even fetching rows
            sent.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd pi && python3 test_weekly_report.py -v`
Expected: `AttributeError: module 'daily_report' has no attribute '_day_coverage_flux'`

- [ ] **Step 3: Implement**

```python
from pathlib import Path as _Path  # already imported as Path — reuse existing import
import report_baseline as rb

STATE_PATH = Path(os.getenv("REPORT_STATE_PATH", "/app/state/anomaly_state.json"))


def _day_coverage_flux(target_date: date) -> str:
    start, stop = local_day_utc_range(target_date)
    return f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {flux_ts(start)}, stop: {flux_ts(stop)})
  |> filter(fn: (r) => r._measurement == "{MEAS_1H}" and r._field == "{COUNTER_FIELD}")
  |> filter(fn: (r) => exists r._value)
  |> group()
  |> distinct(column: "_time")
  |> count()
'''


def query_day_coverage(query_api, target_date: date) -> float:
    """Fraction of the 24 expected circuit_1h hours present for `target_date`
    (Pacific), across any circuit. Gates all anomaly alerting for the day —
    see report_baseline.day_coverage_ok."""
    for table in query_api.query(_day_coverage_flux(target_date), org=INFLUXDB_ORG):
        for rec in table.records:
            return min(1.0, (rec.get_value() or 0) / 24.0)
    return 0.0


def render_anomaly_email(target_date: date,
                         alerts: list[tuple[str, float, "rb.Baseline", "rb.AnomalyResult"]],
                         top_circuits: dict[str, list[tuple[str, float]]]) -> tuple[str, str]:
    """(subject, html). Subject carries the whole message — most days it's
    actionable without opening (spec: 'The anomaly email' > Content)."""
    if len(alerts) == 1:
        cat, value, baseline, result = alerts[0]
        direction = "above" if value > baseline.median else "below"
        subject = (f"⚡ {cat} {result.pct:.0f}% {direction} normal for a "
                  f"{target_date.strftime('%A')}")
    else:
        subject = f"⚡ Unusual usage: {', '.join(c for c, *_ in alerts)}"

    blocks = []
    for cat, value, baseline, result in alerts:
        circuits = ", ".join(f"{n} ({k:.1f} kWh)" for n, k in top_circuits.get(cat, [])[:3])
        blocks.append(f'''<h3>{cat}</h3>
<p>{value:.1f} kWh vs a normal {baseline.median:.1f} kWh for a
{target_date.strftime("%A")} (${value * ENERGY_RATE:.2f}).</p>
<p style="font-size:13px;color:#666;">Driven by: {circuits or "no single circuit stands out"}</p>''')

    html = f'''<!DOCTYPE html>
<html><head><style>{CSS}</style></head>
<body>
<h2>Unusual usage &mdash; {target_date.strftime("%A, %B %-d")}</h2>
{"".join(blocks)}
</body></html>'''
    return subject, html


def generate_anomaly_check(client: InfluxDBClient, target_date: date):
    """Runs daily for `target_date` (the previous local day, at 07:00). Sends
    nothing on a normal day."""
    query_api = client.query_api()
    coverage = query_day_coverage(query_api, target_date)
    if not rb.day_coverage_ok(coverage):
        logger.warning(f"{target_date}: coverage {coverage:.0%} < 90%, suppressing all alerts")
        return

    sample_dates = rb.trailing_same_weekday_dates(target_date, 8)
    fetch_start = flux_ts(local_day_utc_range(sample_dates[0])[0])
    fetch_stop = flux_ts(local_day_utc_range(target_date + timedelta(days=1))[0])
    rows = query_daily_circuit_counter_kwh(query_api, fetch_start, fetch_stop)
    day_cat = category_day_kwh(rows)

    alerts = []
    for category in _all_categories():
        samples = [day_cat[d][category] for d in sample_dates
                  if d in day_cat and category in day_cat[d]]
        if not rb.category_coverage_ok(len(samples)):
            logger.info(f"{category}: only {len(samples)}/8 baseline samples, skipping")
            continue
        baseline = rb.compute_baseline(samples)
        value = day_cat.get(target_date, {}).get(category, 0.0)
        result = rb.evaluate(value, baseline)
        state = rb.load_state(STATE_PATH, category)
        if result.is_anomalous:
            if rb.should_alert(result, state):
                alerts.append((category, value, baseline, result))
                rb.save_state(STATE_PATH, category, target_date, result.z)
        else:
            rb.clear_state(STATE_PATH, category)

    if not alerts:
        return

    top_circuits = {cat: category_top_circuits(rows, local_week_start(target_date), cat, n=3)
                    for cat, *_ in alerts}
    subject, html = render_anomaly_email(target_date, alerts, top_circuits)
    send_email(html, subject)


def main():
    parser = argparse.ArgumentParser(description="Weekly energy report + daily anomaly check")
    parser.add_argument("--loop", action="store_true",
                       help="Run forever: anomaly check daily, weekly briefing Mondays, "
                            "both at REPORT_HOUR local")
    parser.add_argument("--date", type=str,
                       help="Send the weekly briefing for the week containing this date "
                            "(YYYY-MM-DD) — on-demand test send")
    parser.add_argument("--anomaly-date", type=str,
                       help="Run the anomaly check for this date (YYYY-MM-DD) — on-demand test")
    args = parser.parse_args()

    for var, name in [(INFLUXDB_TOKEN, "INFLUXDB_TOKEN"), (RESEND_API_KEY, "RESEND_API_KEY"),
                      (REPORT_EMAIL, "REPORT_EMAIL")]:
        if not var:
            logger.error(f"{name} not set")
            return

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
        logger.info(f"Generating weekly briefing for the week containing {args.date}")
        generate_weekly_report(client, local_week_start(target))
    elif args.anomaly_date:
        target = datetime.strptime(args.anomaly_date, "%Y-%m-%d").date()
        logger.info(f"Running anomaly check for {args.anomaly_date}")
        generate_anomaly_check(client, target)
    elif args.loop:
        logger.info(f"Loop mode: anomaly check daily, weekly briefing Mondays, at {REPORT_HOUR}:00")
        while True:
            wait = seconds_until_hour(REPORT_HOUR)
            logger.info(f"Next run in {wait / 3600:.1f} hours")
            time.sleep(wait)
            yesterday = (datetime.now() - timedelta(days=1)).date()
            try:
                generate_anomaly_check(client, yesterday)
            except Exception as e:
                logger.error(f"Anomaly check failed: {e}")
            if datetime.now().weekday() == 0:   # Monday: yesterday closed last week
                try:
                    generate_weekly_report(client, local_week_start(yesterday))
                except Exception as e:
                    logger.error(f"Weekly report failed: {e}")
    else:
        generate_anomaly_check(client, datetime.now().date() - timedelta(days=1))

    client.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pi && python3 test_weekly_report.py -v` and `python3 test_report_baseline.py -v`
Expected: both PASS.

- [ ] **Step 5: Manual verification**

**Test the anomaly check on demand (won't send unless yesterday was actually anomalous):**

```bash
cd pi && docker compose run --rm daily-report python daily_report.py --anomaly-date 2026-08-17
```

Check the logs for either "coverage ... suppressing" (if the day had a gap), per-category
"only N/8 baseline samples" (early in rollout, before 8 weeks of history exist), or nothing at
all (normal day — this is the expected common case; the email itself is proof, not the log). To
force a visible test end-to-end, temporarily lower the trigger in a scratch Python shell using
`report_baseline.evaluate` directly against real numbers pulled from Influx, rather than editing
the committed thresholds.

- [ ] **Step 6: Commit**

```bash
git add pi/daily_report.py
git commit -m "feat: wire the daily anomaly check into daily_report.py"
```

---

### Task 11: Docker/compose state volume + final docs

**Files:**
- Modify: `pi/docker-compose.yml`
- Modify: `pi/Dockerfile`
- Modify: `/Users/nico/src/SPAN/CLAUDE.md`

**Interfaces:** none (infra + docs).

- [ ] **Step 1: Add a persistent volume for suppression state**

In `pi/docker-compose.yml`, under the `daily-report` service, add:

```yaml
  daily-report:
    build: .
    container_name: daily-report
    restart: unless-stopped
    command: ["python", "-u", "daily_report.py", "--loop"]
    env_file:
      - .env
    environment:
      - INFLUXDB_URL=http://influxdb:8086
      - INFLUXDB_TOKEN=${INFLUXDB_TOKEN}
      - INFLUXDB_ORG=home
      - INFLUXDB_BUCKET=span
      - RESEND_API_KEY=${RESEND_API_KEY}
      - REPORT_EMAIL=${REPORT_EMAIL}
      - TZ=America/Los_Angeles
    volumes:
      - report-state:/app/state
    depends_on:
      - influxdb
```

And add `report-state:` to the top-level `volumes:` block alongside `influxdb-data`,
`influxdb-config`, `grafana-data`.

Deliberately **not** added to `pi/backup/`'s restic backup scope: losing this file only means one
category's suppression window resets early (worst case: one extra alert on the day it's lost), not
data loss. Note this explicitly rather than silently omitting it, in case a future reader wonders.

- [ ] **Step 2: Ship `report_baseline.py` in the image**

In `pi/Dockerfile`, add alongside the other `COPY` lines:

```dockerfile
COPY report_baseline.py .
```

- [ ] **Step 3: Update CLAUDE.md**

Replace the "Weekly energy report" Next Steps bullet in `/Users/nico/src/SPAN/CLAUDE.md` with:

```markdown
- **Weekly energy report + anomaly email** — DONE, shipped 2026-08-2X. Plan:
  `docs/superpowers/plans/2026-08-22-weekly-energy-report.md`. Weekly briefing Mondays 07:00
  (headline, week-by-day chart, 12-week trend, usage table with Unmonitored row, HVAC block);
  daily anomaly check at 07:00, silent unless a category's median/MAD baseline is exceeded.
  Suppression state in `/app/state/anomaly_state.json` on the `report-state` volume. Retired: the
  nine-section daily email, the aux-heat alarm, `pi/rates.py`'s already-flat cost model needed no
  change. Left in place but now unreferenced by the report: the #9 raw/rollup segment router in
  `daily_report.py` (`_run_segments` and friends) — candidate for a future cleanup pass if nothing
  else picks it up.
```

(Fill in the actual ship date when this task is executed.)

- [ ] **Step 4: Full manual verification pass**

**Weekly briefing test send:**

```bash
cd pi && docker compose run --rm daily-report python daily_report.py --date 2026-08-17
```
Pass: email arrives with headline, week-by-day chart, 12-week trend chart, usage table (6 rows),
HVAC block. Fail: any exception in `docker compose run`'s output, or a missing block in the email.

**Anomaly check test send:**

```bash
cd pi && docker compose run --rm daily-report python daily_report.py --anomaly-date 2026-08-17
```
Pass: exits cleanly; an email arrives only if that day was genuinely anomalous (check the log line
either way). Fail: a traceback.

**Full test suite:**

```bash
cd pi && python3 test_daily_report_rollups.py -v && python3 test_weekly_report.py -v && python3 test_report_baseline.py -v
```
Pass: all green.

**Restart the live stack:**

```bash
ssh nico@phrpi.local "cd ~/SPAN/pi && docker compose up -d --build daily-report"
```
Pass: `docker compose ps` on the Pi shows `daily-report` healthy; `docker compose logs daily-report
--tail 20` shows "Loop mode: anomaly check daily, weekly briefing Mondays" with no traceback.

- [ ] **Step 5: Commit**

```bash
git add pi/docker-compose.yml pi/Dockerfile CLAUDE.md
git commit -m "feat: add suppression-state volume, ship report_baseline.py, update docs"
```

---

## Self-Review Notes

**Spec coverage** — every named section of the design doc maps to a task:
- Headline → Task 3, 5
- This week by day → Task 5
- 12-week trend → Task 5
- Usage table + Unmonitored → Task 2, 4, 5
- HVAC block → Task 7
- Baseline (median/MAD) → Task 8
- Trigger (z + floor + degenerate fallback) → Task 8
- Repeat suppression → Task 9
- Coverage guards (day + category) → Task 9, 10
- Test send (`--date` reinterpreted) → Task 5; `--anomaly-date` → Task 10
- Architecture (two files, circuit_1h only, energy_wh_counter) → Global Constraints, Task 1
- Suppression state on a mounted volume → Task 11
- Cost model convergence → confirmed already done (Global Constraints), no task needed
- Phase 4 (hot water/space conditioning split) → explicitly out of scope per spec, no task

**Known simplifications, stated rather than hidden:**
- 12-week and month-over-month averages treat a day with no `circuit_1h` rows as zero usage
  rather than distinguishing "no data" from "no usage" — acceptable per the design's own
  baseline-relative, self-calibrating philosophy (no absolute thresholds to get wrong), and
  consistent with the existing `avg30_excl` pattern already in the codebase.
- The anomaly check does not evaluate "Unmonitored" — the spec's anomaly section only discusses
  per-category baselines, and Unmonitored is a residual accounting row, not a metered category
  with a coherent baseline shape of its own.
- Chart rendering (matplotlib output) is manual-verify only, consistent with how `web/` treats its
  chart layer and the spec's own "Testing" section.
