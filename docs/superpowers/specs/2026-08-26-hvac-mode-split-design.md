# HVAC mode timeline: heat / cool / hot-water split (#14 sub-project 2)

**Date:** 2026-08-26
**Status:** approved design, pre-implementation
**Approach:** timeline-first ("option B") — one mode classifier producing a labeled
timeline; the split, bath events, and the web breakdown are all views of it.
**Principles:** elegance and simplicity; TDD throughout; YAGNI (weekly-email split,
shower/laundry predicates, and Timescale stay in issues, not in scope).

## Problem

The Stiebel Eltron runs space heating, cooling, and domestic hot water (DHW) on the
same two circuits (`Heat pump (HP)`, `Auxiliary / Heat pump (HP)`), so the breakdown
can't say what the HP energy was *for*. Power signature alone can't separate heating
from cooling; outdoor temperature (shipped in #14 Phase 1, backfilled to 2026-01-04)
makes it classifiable. `bath_detector.py` is a hardcoded special case of the general
capability (attribute HP energy to a cause) and should be re-based, not duplicated.

## Deliverables

1. A derived `hvac_mode` series in Influx, one point per 5-minute interval,
   backfilled to 2026-01-04, kept current by a Pi service.
2. The web breakdown's HVAC row splits into Heating / Cooling / Hot Water sub-rows.
3. `bath_detector.py` re-based onto a generic run-grouping attribution module,
   with output parity against the existing `bath_event` history.

Out of scope (tracked in issues, not here): weekly-email split, shower and laundry
hot-water predicates, water-bill estimate, Timescale migration (#18), weather_poller
health detection.

## Architecture

Three new/changed units on the Pi plus one web change. Pure logic and I/O are
separated in the pattern #16 established (`collector_health.py` vs `collector.py`).

### `pi/hvac_modes.py` — pure classifier (new)

No I/O. Input: raw 30s power samples for HP + aux over a span, hourly outdoor temp
(`weather` measurement values). Output: a list of 5-minute intervals, each:

```python
{"start": dt, "mode": str, "energy_kwh": float, "mean_power_w": float,
 "cost_dollars": float}
```

`mode ∈ {heat, cool, hot_water, idle, ambiguous}`. Two-stage classification:

- **Stage 1 — DHW (season-invariant).** Hot-water reheats are identified from the
  power shape inside/around the interval — sustained high draw, high duty cycle, few
  transitions, bounded duration — the same stats `bath_detector.analyze_window`
  computes today. Thresholds come from Phase 0, not from this document.
- **Stage 2 — heat vs cool (weather-resolved).** Non-DHW active intervals split by
  outdoor temperature band. Mid-band → `ambiguous`, never a guess. Band edges come
  from Phase 0.
- Below an activity floor → `idle`. Missing weather data for an active interval →
  `ambiguous` (DHW can still be labeled; it doesn't need weather).

Costs use `rates.cost_for_kwh`, as the detectors do today.

### `hvac_mode` measurement — storage

Bucket `span`, measurement `hvac_mode`, **no tags**, one point per 5-minute
interval timestamped at interval start (UTC). Fields:

- `energy_heat_kwh`, `energy_cool_kwh`, `energy_hot_water_kwh`,
  `energy_idle_kwh`, `energy_ambiguous_kwh` — the interval's energy lands in
  exactly one of these; the rest are 0.0. (Amended 2026-08-26: the original
  draft used a `mode` tag, but tags split series identity, so a re-classified
  interval would leave its stale point behind and double-count — the exact trap
  `write_weather_points`' docstring documents. Fields overwrite; tags don't.)
- `mode` (string) — the label, for Grafana/debug legibility.
- `hp_mean_w`, `hp_max_w`, `aux_mean_w`, `aux_max_w` — per-circuit stats, so the
  re-based bath detector can emit schema-compatible `bath_event`s from the
  timeline alone.
- `cost_dollars`.

Properties:

- **Idempotent by construction:** with no tags, series identity is fixed, so
  Influx overwrites on identical timestamp — re-classification (backfill
  re-runs, threshold revisions, self-heal) is plain re-writing. No dedup logic.
- Interval width 5 min, aligned to the wall clock, matching `circuit_5m`.
- Sizing: ~105k intervals/yr → negligible against ~460 MB/yr raw and 90 GB free.
- Covered by the existing nightly restic backup (whole InfluxDB volume).

### `pi/hvac_classifier.py` — service (new)

Thin I/O wrapper mirroring `weather_poller.py`'s CLI shape:

- `--loop` (default interval 600s): each pass re-classifies the trailing ~3h and
  writes all of it. Overwrite semantics make a missed pass self-healing, same idea
  as weather's `past_days=2`.
- `--backfill [--start-date 2026-01-04]`: day-by-day batch over history.
- `--backtest [--days N]`: classify and print, no writes — the Phase 0 tool.

Deploy: new service block in `pi/docker-compose.yml` + **Dockerfile COPY lines for
every new `pi/*.py` file** (known deploy gotcha), `restart: unless-stopped`.

### `pi/attribution.py` — run grouping (new, pure)

No I/O. `runs(intervals, mode)` groups consecutive same-mode intervals into runs
(tolerating single-interval dropouts is a Phase 0 decision, default: no tolerance).
An event detector is a predicate over runs. One predicate ships now:

- `bath(run)`: `hot_water` run with duration and mean-power bounds (values from
  Phase 0, seeded from today's constants: ≥ 3 windows ≈ 25 min, mean ≥ 2500 W).

### `pi/bath_detector.py` — re-based (changed)

Becomes a thin service: query the `hvac_mode` timeline (not raw circuits), apply
`attribution.bath`, write `bath_event` with the **same schema, same ±2h dedup, same
CLI flags**. `daily_report.py` and its email need zero changes. The windowing/stats
code it loses lives on in `hvac_modes.py`. Loop cadence unchanged (10 min); it now
depends on the classifier's output lagging ≤ ~15 min, which the 3h trailing window
comfortably covers.

### `web/` — breakdown sub-rows (changed)

- New query in `web/lib/influx.ts`: sum the three `energy_*_kwh` mode fields of
  `hvac_mode` over the window (group by `_field`). Cheap (pre-aggregated 5-min points), cached like the breakdown —
  server `queryCache` + client `TtlLru`, keyed by window only.
- `/api/energy` category view returns Heating / Cooling / Hot Water rows carrying
  `parent: "HVAC"`, reusing the existing parent-nesting mechanism.
- The HVAC top-level row is **unchanged** — still circuit-integral energy — so
  whole-house reconciliation (incl. Unmonitored) is untouched. The gap between the
  sub-rows' sum and the HVAC total (idle + ambiguous + any timeline gap) is simply
  the parent's remainder; nothing is invented or double-counted.
- Pure merge/nesting logic unit-tested in vitest; chart wiring stays manual-verify,
  per house style.

## Phase 0 — exploration gates everything

First deliverable: `hvac_classifier.py --backtest` over January→now raw + weather
data, iterating thresholds until three ground-truth gates pass:

1. **Bath parity:** re-based detection reproduces the historical `bath_event` set,
   target ≥ 95% match; every diff individually examined (the old detector is not
   ground truth either — diffs may be its bugs).
2. **Seasonal sanity:** ~zero `heat` energy during the hottest July days, ~zero
   `cool` in deep January.
3. **Energy conservation:** per-day sum of mode energies (incl. idle/ambiguous) vs
   HP+aux circuit energy within ~2% on gap-free days.

Output: threshold constants checked in with a short findings note (where each number
came from). **If DHW proves inseparable in winter, stop and redesign** — that risk
is retired in Phase 0, before the pipeline is built.

Explicit hedge: the DHW signature and temp bands in this spec are *shapes*, not
values. Phase 0 supplies the values; the spec deliberately contains no magic numbers
beyond seeds.

## Testing (TDD)

Pure modules first, test-first:

- `pi/test_hvac_modes.py` — synthetic sample streams: pure-heating day, pure-cooling
  day, bath-in-winter, bath-in-summer, idle, data gaps, missing weather,
  mid-band temps, negative power values (abs() handling as in `analyze_window`).
- `pi/test_attribution.py` — run grouping edges: empty, single interval, runs at
  span boundaries, adjacent different-mode runs, bath predicate bounds.
- `web/lib` — vitest units for the mode-row merge/nesting and cache-keying, in the
  style of `energyWindow.test.ts`.
- Phase 0 gates are the integration tests; they run against real data.

## Definition of done

- Phase 0 gates pass; findings note committed.
- Backfill to 2026-01-04 completed on the Pi; spot-checked for gaps.
- Classifier + re-based bath detector deployed via compose; both healthy over a
  live-verification window; a real bath detected end-to-end post-re-base.
- Web sub-rows live at https://span.pianohouseproject.org with numbers that
  reconcile against the HVAC row.
- Old raw-circuit path in `bath_detector.py` deleted, not flagged off.

## Machine placement

Everything on the Pi, per the 2026-08-13 machine-roles decision (Mini stays
service-free; revisit only if #18 lands). Compute and storage costs are trivial.

## Decisions log

- Timeline-first over event-first: the split, events, and presentation become views
  of one primitive; sub-slices can't double-count by construction.
- Influx over Timescale pilot: #18 hasn't started; weather already went Influx; the
  web path is Flux. #18 migrates this series later if it lands.
- Nested sub-rows over replacing the HVAC row: keeps reconciliation trivially true
  and matches the roadmap's "derived sub-slices, not peers" rule.
- Free retro answer: the recirc-pump question (unplugged 2026-04-09, $107–$850/yr
  bounded) becomes readable from overnight `hot_water` energy before/after the
  unplug date on the backfilled timeline. Worth a look after backfill; not a gate.
