# Make `energy_wh_counter` Authoritative — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch every *energy* read in the dashboard and the report from `energy_wh` (a 30s `integral()` that invents energy across missed polls) to `energy_wh_counter` (SPAN's own cumulative meter delta, immune to missed polls).

**Architecture:** Both fields are already written side by side into `circuit_5m` and `circuit_1h` by the rollup tasks from #9, specifically so this could be decided on real data. No new collection, no schema change, no backfill. The change is: evaluate, then flip a field name in two consumers, then document the contract.

**Tech Stack:** InfluxDB 2 / Flux, Python 3 (`unittest`), TypeScript / Next.js (`vitest`)

**Spec:** `docs/superpowers/specs/2026-08-21-weekly-energy-report-design.md` (§ Data source). Closes GitHub issue #15.

## Global Constraints

- **Charts keep the integral.** `energy_wh_counter` is too coarse for fine buckets — three consecutive 30s samples have been observed reading an identical value. Only *energy totals* switch. `power_w` / `power_w_mean` paths are untouched.
- **Windows ≤48h keep raw integral.** `ENERGY_RAW_MAX_MS` already routes short windows to raw `power_w` integration. That stays; it is the same "counter is too coarse" concern, already handled.
- **`energy_wh` stays stored.** It is cheap and remains useful as a cross-check. Nothing is deleted from the rollup tasks.
- **Never use Flux `increase()` on the counter.** `nonNegative: true` treats SPAN's small backward corrections as a counter wrap and returns the current value — turning a 14.43 Wh hour into 113,114 Wh. The rollup tasks already use `difference(nonNegative: false)` → drop negatives → `sum()`. Consumers read the *already-corrected* rollup field, so they need no reset handling of their own — but no consumer may re-derive from the raw counter.
- **`circuit_1h` / `circuit_5m` timestamps are end-of-bucket.** A point at T covers `[T − bucket, T)`. See `pi/influx_tasks/README.md`.
- Run Python tests with `cd pi && python3 test_daily_report_rollups.py`. Run web tests with `cd web && npm test`.
- Influx is not reachable from the laptop (Cloudflare Access). Query it via `ssh nico@phrpi.local` and `docker compose exec -T influxdb influx query --org home '<flux>'`.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `pi/daily_report.py` | Report queries | Switch the **energy value** field only (line ~265). Two other `energy_wh` filters are probes and must NOT change — see Task 2. |
| `pi/test_daily_report_rollups.py` | Rollup routing tests | Update the field assertion; add a regression test pinning the probe/value distinction. |
| `web/lib/rollup.ts` | Energy source routing | Widen the `field` union; switch both rollup sources. |
| `web/lib/rollup.test.ts` | Routing tests | Update expected field. |
| `pi/influx_tasks/README.md` | Rollup contract | Document which field is authoritative and why. |
| `CLAUDE.md` | Project docs | Update the #9/#15 notes. |

---

### Task 1: Evaluate the two fields across the backfilled history

This is the decision gate from #15 (“Compare the two over the full backfilled history, especially days with known collector gaps”). It produces evidence, not code. **If the evidence contradicts the expected pattern, stop and report rather than proceeding to Task 2.**

**Files:**
- Create: `pi/tools/compare_energy_fields.sh`

**Interfaces:**
- Consumes: nothing
- Produces: a findings summary pasted into issue #15. No code artifact other tasks depend on.

- [ ] **Step 1: Write the comparison script**

Create `pi/tools/compare_energy_fields.sh`:

```bash
#!/usr/bin/env bash
# Compare circuit_1h.energy_wh (integral) against energy_wh_counter (meter
# delta) per day, alongside raw poll coverage. #15.
#
# Run ON the Pi:  bash compare_energy_fields.sh
set -euo pipefail
cd ~/SPAN/pi

echo "=== daily totals: energy_wh vs energy_wh_counter ==="
docker compose exec -T influxdb influx query --org home '
from(bucket: "span")
  |> range(start: -180d)
  |> filter(fn: (r) => r._measurement == "circuit_1h")
  |> filter(fn: (r) => r._field == "energy_wh" or r._field == "energy_wh_counter")
  |> filter(fn: (r) => exists r._value)
  |> group(columns: ["_field"])
  |> aggregateWindow(every: 1d, fn: sum, createEmpty: false)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({ r with pct_integral_over_counter:
        100.0 * (r.energy_wh - r.energy_wh_counter) / r.energy_wh_counter }))
  |> keep(columns: ["_time", "energy_wh", "energy_wh_counter", "pct_integral_over_counter"])
'

echo
echo "=== raw poll coverage per day (points across all circuits) ==="
docker compose exec -T influxdb influx query --org home '
from(bucket: "span")
  |> range(start: -180d)
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> group()
  |> aggregateWindow(every: 1d, fn: count, createEmpty: false)
'
```

- [ ] **Step 2: Run it on the Pi**

```bash
scp pi/tools/compare_energy_fields.sh nico@phrpi.local:~/
ssh nico@phrpi.local 'bash ~/compare_energy_fields.sh' | tee /tmp/energy-compare.txt
```

- [ ] **Step 3: Check the evidence against the expected pattern**

Expected, from #15's 2026-07-25 sample (2,822 of 2,880 polls, ~2% missed):

- On **gap-free** days the two fields agree closely — the reference figure is `circuit_1h.energy_wh` at −0.002% vs raw integral on the 07-17→07-24 gap-free week.
- On **gappy** days `energy_wh` reads **higher** than `energy_wh_counter`, because the integral interpolates a straight line across the gap and invents energy. The 07-25 sample: 40,825.00 vs 40,709.24 Wh, a +0.28% integral excess.
- Therefore `pct_integral_over_counter` should **correlate with low poll coverage** — near zero on full-coverage days, rising as coverage drops.

Confirm three things and write down the numbers:

1. The worst-coverage days show the largest positive `pct_integral_over_counter`.
2. Full-coverage days show a near-zero spread.
3. No day shows the counter wildly exceeding the integral — a large *negative* value would suggest an unhandled counter reset and **must** be investigated before proceeding.

- [ ] **Step 4: Post the findings to #15**

```bash
gh issue comment 15 --body "$(cat <<'BODY'
## Evaluation over full backfilled history (Task 1)

<paste the daily table, or a representative excerpt plus the summary stats>

- Gap-free days: spread of <X>%
- Worst-coverage day (<date>, <N> polls): integral read <Y>% above counter
- Correlation between missed polls and integral excess: <confirmed / not confirmed>
- Negative excursions suggesting unhandled counter resets: <none / details>

Proceeding to switch consumers to `energy_wh_counter`.
BODY
)"
```

- [ ] **Step 5: Commit the tool**

```bash
git add pi/tools/compare_energy_fields.sh
git commit -m "tools: script to compare energy_wh vs energy_wh_counter (#15)"
```

---

### Task 2: Switch `pi/daily_report.py` to the counter

**Files:**
- Modify: `pi/daily_report.py:265`
- Modify: `pi/test_daily_report_rollups.py:208-210`
- Test: `pi/test_daily_report_rollups.py`

**Interfaces:**
- Consumes: Task 1's confirmation that the counter is the better source.
- Produces: `_circuit_kwh_flux(..., mode="energy")` emits Flux filtering `_field == "energy_wh_counter"`. Every energy helper in the report inherits this; no other signature changes.

**Critical — three `energy_wh` filters exist and only ONE changes:**

| Line | Function | Purpose | Change? |
|---|---|---|---|
| ~183 | `rollup_span()` | Existence/extent probe. Only reads `_time`. | **No** |
| ~225 | `_rollup_stamp()` | Calibrates rollup tail lag by comparing rollup sums against the **raw integral**. | **No** — switching it would introduce a systematic integral-vs-counter offset that the function would misread as lag. |
| ~265 | `_circuit_kwh_flux()` | The actual energy value path. | **Yes** |

- [ ] **Step 1: Write the failing test**

In `pi/test_daily_report_rollups.py`, replace the body of `test_rollup_sums_energy_wh_instead_of_integrating` (line ~208) and add a companion test directly after it:

```python
    def test_rollup_sums_energy_wh_counter_instead_of_integrating(self):
        flux = daily_report._circuit_kwh_flux(
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
        src = inspect.getsource(daily_report._rollup_stamp)
        self.assertIn('_field == "energy_wh"', src)
        self.assertNotIn('energy_wh_counter', src)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd pi && python3 test_daily_report_rollups.py`
Expected: FAIL on `test_rollup_sums_energy_wh_counter_instead_of_integrating` — the assertion finds `energy_wh` where `energy_wh_counter` was expected. The lag-probe test should already PASS (it is a regression guard).

- [ ] **Step 3: Make the change**

In `pi/daily_report.py`, line ~265, inside `_circuit_kwh_flux()`:

```python
    field = "power_w" if raw else ("energy_wh_counter" if mode == "energy" else "power_w_mean")
```

Update the docstring three lines above it, which currently says "a rollup just sums its precomputed energy_wh":

```python
    """Pipeline yielding circuit energy in kWh — one value per `every`-window, or
    one per series when `every` is None. Raw integrates 30s power exactly as
    before #9; a rollup just sums its precomputed energy_wh_counter — SPAN's own
    meter delta, which is immune to missed polls (#15). `mode="mean"`
    reproduces the mean-power-times-window form the hourly query has always used.
```

Also update the module comment at line ~81-83, which describes the rollup contract:

```python
# `circuit_5m` / `circuit_1h`, each bucket carrying `energy_wh` (integral of
# |power_w| over the bucket, in Wh), `energy_wh_counter` (delta of SPAN's own
# cumulative meter — authoritative for energy since #15, immune to missed polls)
# and `power_w_mean`. Summing energy_wh_counter costs
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd pi && python3 test_daily_report_rollups.py`
Expected: PASS, all tests (was 25, now 26).

- [ ] **Step 5: Commit**

```bash
git add pi/daily_report.py pi/test_daily_report_rollups.py
git commit -m "report: read energy from energy_wh_counter, not the integral (#15)

The integral interpolates across missed polls and invents energy that was
never measured. SPAN's cumulative meter keeps ticking whether or not we
poll, so its delta is immune.

Only the energy VALUE path moves. rollup_span() is an existence probe,
and _rollup_stamp() calibrates tail lag against the raw integral — giving
it the counter would show a constant offset it would misread as lag. A
regression test pins that distinction."
```

---

### Task 3: Switch `web/lib/rollup.ts` to the counter

**Files:**
- Modify: `web/lib/rollup.ts:106`, `web/lib/rollup.ts:139-155`
- Modify: `web/lib/rollup.test.ts:81`
- Test: `web/lib/rollup.test.ts`

**Interfaces:**
- Consumes: nothing from Task 2 — the two consumers are independent.
- Produces: `energySourceForSpan(spanMs)` returns `field: "energy_wh_counter"` for both `circuit_5m` and `circuit_1h`. `RAW_ENERGY_SOURCE` is unchanged (`field: "power_w"`, `mode: "integral"`). `web/lib/influx.ts` reads `source.field` and needs no edit.

- [ ] **Step 1: Write the failing test**

In `web/lib/rollup.test.ts`, make one in-place edit and add two new tests.

**In-place edit:** inside the existing `it("sums the 5m rollup just past 48h and up to 7d inclusive", ...)` there is a single `field: "energy_wh",` line (~line 81). Change that one value to `field: "energy_wh_counter",`. Do **not** delete or replace the surrounding test — it covers boundary routing that still needs to pass.

**Then add**, after that block:

```ts
  it("sums the counter, not the integral, for rollup-backed windows", () => {
    const fiveMin = energySourceForSpan(3 * DAY_MS);
    expect(fiveMin.measurement).toBe("circuit_5m");
    expect(fiveMin.field).toBe("energy_wh_counter");
    expect(fiveMin.mode).toBe("sum");

    const hourly = energySourceForSpan(30 * DAY_MS);
    expect(hourly.measurement).toBe("circuit_1h");
    expect(hourly.field).toBe("energy_wh_counter");
    expect(hourly.mode).toBe("sum");
  });

  it("keeps short windows on the raw integral", () => {
    // The counter is too coarse for fine buckets — three consecutive 30s
    // samples have been seen reading an identical value (#15).
    const short = energySourceForSpan(12 * HOUR_MS);
    expect(short.measurement).toBe("circuit");
    expect(short.field).toBe("power_w");
    expect(short.mode).toBe("integral");
  });
```

`DAY_MS` and `HOUR_MS` are already exported from `./rollup` and already imported at the top of this test file — no import changes needed.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd web && npm test -- rollup`
Expected: FAIL — received `"energy_wh"`, expected `"energy_wh_counter"`.

- [ ] **Step 3: Widen the type**

In `web/lib/rollup.ts`, line ~106:

```ts
  field: "power_w" | "energy_wh" | "energy_wh_counter";
```

- [ ] **Step 4: Switch both rollup sources**

In `energySourceForSpan()`, change both returned `field` values from `"energy_wh"` to `"energy_wh_counter"`. Leave `RAW_ENERGY_SOURCE` alone. Then update the doc comment above the function:

```ts
/**
 * Energy source by window span:
 *
 *   ≤ 48h → raw integral (existing pipeline, most accurate at fine buckets)
 *   ≤ 7d  → sum(circuit_5m.energy_wh_counter)
 *   > 7d  → sum(circuit_1h.energy_wh_counter)
 *
 * Summing pre-computed Wh is *exact* — there is no re-integration error — which
 * is why this is the big win over `queryPower`'s mean-of-means.
 *
 * Since #15 the summed field is `energy_wh_counter`, the delta of SPAN's own
 * cumulative meter, rather than `energy_wh`, the integral of our 30s samples.
 * The counter keeps ticking inside the panel whether or not we poll, so a
 * missed poll costs nothing; the integral interpolates across the gap and
 * invents energy that was never measured. `energy_wh` is still stored as a
 * cross-check. Short windows stay on the raw integral because the counter is
 * too coarse for fine buckets.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd web && npm test`
Expected: PASS, full suite.

- [ ] **Step 6: Commit**

```bash
git add web/lib/rollup.ts web/lib/rollup.test.ts
git commit -m "web: sum energy_wh_counter for rollup-backed windows (#15)

Windows over 48h now sum SPAN's meter delta rather than the integral of
our own samples, so missed polls no longer inflate energy totals. Short
windows stay on the raw integral — the counter is too coarse for fine
buckets."
```

---

### Task 4: Document the contract and close #15

**Files:**
- Modify: `pi/influx_tasks/README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: Tasks 2 and 3 complete.
- Produces: nothing code-facing.

- [ ] **Step 1: Update the rollup contract**

Add to `pi/influx_tasks/README.md`, in the section describing the stored fields:

```markdown
### Which energy field to read

**`energy_wh_counter` is authoritative for energy** (decided #15, 2026-08-21).
It is the delta of SPAN's own cumulative `consumedEnergyWh`, which keeps ticking
inside the panel whether or not the collector is listening — so a missed poll
costs nothing. `energy_wh` is the integral of our 30s `power_w` samples, and
InfluxDB interpolates a straight line across any gap, inventing energy that was
never measured.

`energy_wh` is still stored and is useful as a cross-check. Do not delete it.

Two exceptions, both because the counter is too coarse at fine resolution
(three consecutive 30s samples have been observed reading an identical value):

- Windows ≤48h (`ENERGY_RAW_MAX_MS`) integrate raw `power_w` instead.
- Charts read `power_w` / `power_w_mean`, never either energy field.

**Never apply Flux `increase()` to the counter.** `nonNegative: true` treats
SPAN's small backward corrections — observed −5.59 Wh across all 21 circuits
simultaneously at a panel restart, twice in one sampled week — as a counter
wrap, and returns the current value: a 14.43 Wh hour becomes 113,114 Wh. The
rollup tasks use `difference(nonNegative: false)` → drop negatives → `sum()`.
Consumers read the already-corrected rollup field and need no reset handling,
but must not re-derive from the raw counter.

One more trap: `_rollup_stamp()` in `pi/daily_report.py` calibrates tail lag by
comparing rollup sums against the raw integral. It deliberately still reads
`energy_wh` — giving it the counter would show a constant integral-vs-counter
offset that it would misread as lag.
```

- [ ] **Step 2: Update CLAUDE.md**

In the `#9` bullet under Next Steps, replace the `energy_wh_counter` sub-bullet:

```markdown
  - Fields: `power_w_mean`, `energy_wh` (integral), `energy_wh_counter` (SPAN's own
    meter delta). **`energy_wh_counter` is authoritative for energy since #15** — the
    integral invents energy across missed polls, the counter cannot. `energy_wh` is
    retained as a cross-check. Windows ≤48h still integrate raw; charts still read
    power. Contract: `pi/influx_tasks/README.md`.
```

`#15` appears in `CLAUDE.md` at exactly one place — line ~100, inside the `#9` bullet, currently reading "Both energy fields stored deliberately to A/B — see **#15**, which argues the counter should win because it's immune to missed polls." That sentence is what the replacement above supersedes; there is no other `#15` reference to clean up.

- [ ] **Step 3: Commit**

```bash
git add pi/influx_tasks/README.md CLAUDE.md
git commit -m "docs: record energy_wh_counter as authoritative (#15)"
```

- [ ] **Step 4: Verify the dashboard still works end to end**

Deploy is automatic on push. After pushing, check that a rollup-backed window returns data:

```bash
curl -s "https://span.pianohouseproject.org/api/energy?from=$(( ($(date +%s) - 30*86400) * 1000 ))&to=$(( $(date +%s) * 1000 ))" | head -c 300
```

Expected: JSON with per-category energy rows. Then load the dashboard, select the 30d preset, and confirm the breakdown table shows non-zero kWh for every category.

https://span.pianohouseproject.org

Fail: empty `data`, zeros across the table, or a 500 — which would mean the field name is wrong for that measurement. Check the field actually exists:

```bash
ssh nico@phrpi.local 'cd ~/SPAN/pi && docker compose exec -T influxdb influx query --org home "import \"influxdata/influxdb/schema\" schema.fieldKeys(bucket: \"span\", predicate: (r) => r._measurement == \"circuit_1h\")"'
```

- [ ] **Step 5: Close the issue**

```bash
gh issue close 15 --comment "Done. Both consumers now sum energy_wh_counter for rollup-backed windows; energy_wh retained as a cross-check; short windows and all charts unchanged. Contract documented in pi/influx_tasks/README.md, including the increase() trap and the _rollup_stamp() exception."
```

---

## Notes for the next plan

The weekly report (spec Phase 1) builds directly on this. Once this lands, every
energy number the report shows — week-over-week, month-over-month, vs 12-week
average — is measuring usage rather than partly measuring collector reliability.
That plan should not be written until Task 1's evidence is in, since a surprise
there would change the report's data source.
