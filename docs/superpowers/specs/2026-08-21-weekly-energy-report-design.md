# Weekly energy report + anomaly email — design

**Date:** 2026-08-21
**Status:** design approved, not yet implemented
**Supersedes:** the current nine-section daily email in `pi/daily_report.py`

## Problem

The daily report has grown to nine sections and 1366 lines. It arrives every
morning whether or not anything happened, and its content answers questions
nobody asked: individual bath events, individual EV charge sessions, a cost
breakdown against a rate model that disagrees with the dashboard's.

What it does not answer is the question actually worth asking every week: **how
is my usage changing, and what changed it.** HVAC week-over-week and
month-over-month, the five categories' weekly trend, this week broken out by
day.

The brief, from Nico: *"much more informative, with less info."*

## Goals

- One substantial **weekly briefing** that shows how usage is moving.
- A **daily email only when something is unusual** — silent on a normal day.
- Merge "usage by category" and "top circuits" into one thing.
- Visuals and tables both; stacked histograms for category-over-time.
- Converge the report's cost model with the dashboard's.

## Non-goals

- Rate-plan shopping. Dropped 2026-08-21; the billed plan is flat SCL "Small
  General Energy" and that is what both surfaces will use.
- Aux-heat alarming. Explicitly retired (see Decisions).
- Any change to the bath or charge **detectors** in this work. They keep
  running and keep writing. (Phase 4 does touch detection, and is scoped as a
  separate effort — see Out of scope.)
- Making the email configurable from a link (#8). Still deferred.

## Decisions

Each of these was decided in the 2026-08-21 design session. Recorded with
rationale so they are not silently relitigated.

**Cadence: weekly, plus exception.** Weekly briefing Mondays 07:00. A daily
email only when a category is anomalous. Everything Nico asked for is weekly or
monthly in shape; a daily email carrying weekly trends says the same thing seven
days running and trains the reader to ignore it.

**Five categories as-is; Auxiliary is not split out.** Considered splitting
`Auxiliary` from `Heat pump` so the baseline detector could see resistance heat
move independently. Rejected in favour of simplicity. **Consequence, accepted:**
aux-heat detection goes away entirely with the alarm, and the HVAC cooling watch
stays a manual `pi/hvac_probe.py` exercise.

**Baseline-relative anomalies only.** No absolute thresholds, no encoded failure
modes, no "something stopped" detection. Self-calibrating, nothing to tune.
**Consequence, accepted:** failures of absence — a dead freezer, a tripped
breaker on a circuit that normally runs — are structurally invisible to this
design.

**Report moves to flat SCL costing.** `pi/rates.py`'s TOU model is retired for
reporting purposes, converging with `web/lib/rates.ts`. Closes the divergence
where the dashboard and the email disagreed on what a kWh cost.

**No burn-in daily send.** An on-demand test send covers format validation
instead.

## The weekly briefing

Sent Mondays 07:00, covering the previous Monday–Sunday. Five blocks.

### 1. Headline

One line. Week total kWh and cost, Δ vs prior week, Δ vs 12-week average, and
the single largest mover named explicitly.

### 2. This week by day

Seven bars, stacked by category. This is the existing `today_chart` — the one
part of the current email Nico likes — given a week of context instead of a
single day.

### 3. 12-week trend

Stacked histogram of weekly totals by category. Composition and direction in one
image. Twelve weeks is enough to show a season turning without becoming
unreadable at email width.

### 4. Usage table

One table, replacing both `section_cost_breakdown` and `section_top_circuits`.
Five category rows, each with its top circuits nested beneath. Columns:

    kWh | cost | Δ vs last week | Δ vs 12-week avg

Plus an **Unmonitored** row: panel total minus the sum of known circuits.

The panel does not meter everything — there is no washer, dryer, or water heater
circuit (see #17, overflow subpanel). Without this row the category percentages
are wrong in a way the reader cannot see. One row makes the rest trustworthy.

*Assumption to verify during implementation:* that panel-level total energy is
stored and queryable alongside per-circuit energy. `query_total_kwh()` in
`daily_report.py` suggests it is.

### 5. HVAC block

The section Nico led with. This week by day, week-over-week, month-over-month.

Renders without the hot-water split and gains a row when that lands — the email
must not block on the detector work.

## The anomaly email

Runs 07:00 daily for the previous day. **Sends nothing on a normal day.**

### Baseline

Per category, per weekday. Tuesdays compare to Tuesdays: weekday and weekend
shapes differ enough that a flat 7-day average washes out both.

Sample window is the **trailing 8 same-weekdays** (~8 weeks). Four was
considered and rejected — MAD over four samples is too unstable to threshold
against. Seven months of `circuit_1h` history is available, so eight is free.

Statistic is **median and MAD**, not mean and standard deviation. A single EV
charging session inflates σ enough to mask everything else for a month; the
median shrugs it off.

    m     = median(samples)
    mad   = median(|x - m| for x in samples)
    scale = 1.4826 * mad          # normal-consistent
    z     = (value - m) / scale   # when scale > 0

### Trigger

Fires when **both** hold:

    |z| > 3
    |value - m| > max(0.20 * m, 1.0 kWh)

The second is a floor. Without it, Lights alerts on a 0.3 kWh wobble.

Degenerate case: when `mad == 0` (a perfectly regular category), `z` is
undefined. Fall back to a percentage test — fire when
`|value - m| > max(0.50 * m, 1.0 kWh)`.

### Content

Subject carries the whole message, so most days it can be actioned without
opening: `⚡ HVAC 62% above normal for a Tuesday`.

Body: which category, how far off, what normal looks like, the day's shape, and
which circuits within that category drove it.

### Guard: repeat suppression

A heat wave makes HVAC anomalous for six consecutive days. Six emails is the
failure this whole design exists to avoid — and is precisely the failure mode
that consumed 2026-08-21 (a flapping Cloudflare outage produced ~30 UptimeRobot
emails).

Rule: do not re-alert a category within **3 days** unless the deviation
materially worsens (`|z|` grows by more than 25%). State clears when the
category returns to normal (`|z| < 3`).

### Guard: coverage check

A collector outage makes every category look anomalously *low*. On 2026-08-21 the
Pi stayed healthy while the read path failed for hours — the inverse case, but
the same lesson: a monitoring system must distinguish "I could not look" from
"the value is bad."

Two rules:

- Day-level: require ≥90% of expected hourly points present for the day. Below
  that, **suppress all alerting for that day.**
- Category-level: require ≥6 of 8 baseline samples present. Below that, skip
  that category.

## Architecture

Two files. The seven-module split originally proposed was rejected as
over-engineering.

    pi/daily_report.py     entrypoint, queries, blocks, send (existing)
    pi/report_baseline.py  NEW — median/MAD, weekday bucketing, suppression

Only the baseline math is extracted, because it is the one piece that is pure,
non-obvious, and worth testing properly. Everything else stays where it is.

`daily_report.py` shrinks on net: the aux alarm, bath section, charge section,
and TOU rate model all leave; two chart blocks and one table arrive.

### Data source

Everything reads the **`circuit_1h` rollup**, never raw. It is already built and
verified to −0.0032% against raw integral. A 12-week stacked chart off raw 30s
points would be a punishing query.

**Energy field: `energy_wh_counter`, not `energy_wh`** — decided 2026-08-21.
This report is built entirely on deltas: week-over-week, month-over-month, vs
12-week average. `energy_wh` is a 30s `integral()` and undercounts whenever a
poll is missed, so some fraction of every delta would be measuring collector
reliability rather than usage. `energy_wh_counter` is SPAN's own meter delta and
is immune to missed polls. Both fields are already stored for exactly this A/B.

**This makes #15 a prerequisite, not a follow-up.** Resolving #15 — evaluating
the two fields and making the counter authoritative — lands *before* the report
is built on top of it. Building a trend report on the integral and switching the
energy source underneath it later was considered and rejected: it would silently
change every historical comparison the report had already shown.

Invariants from `pi/influx_tasks/README.md` that apply:

- Timestamps are **end-of-bucket**: a point at T covers `[T − 1h, T)`.
  Comparison queries must shift by one bucket or edges silently mismatch.
- `circuit_1h` tail lag is 5–65 minutes. A 07:00 run reporting on yesterday is
  well clear.

### Test send

`daily_report.py` already accepts `--date`. Extend it so a date selects the week
containing that date and renders the new briefing, sent on demand. This is how
the format gets validated and tuned without touching the schedule — it replaces
the burn-in daily send that was considered and dropped.

### Suppression state

A small JSON file on a mounted volume — containers restart, and the state must
survive. Holds last-alert date and last `|z|` per category. Not a database.

## Testing

The baseline math is pure and gets real unit tests:

- median/MAD, including the `mad == 0` fallback
- the floor, at category scales from Lights to Car
- weekday bucketing across a DST boundary
- **a six-day heat wave produces one alert, not six**
- **a coverage gap produces zero alerts, not five**

Chart and HTML rendering stay manual-verify, consistent with how `web/` treats
its chart layer.

Fixture source: capture 12 weeks of real `circuit_1h` data once to a file.

## Error handling

- Missing or partial data **suppresses** alerts rather than inventing them.
- Failure to reach Influx logs and exits. It never sends a mail claiming
  everything is fine, and never retries into a mail storm.

## What this retires

- `section_aux_alarm` and `AUX_HEAT_ALARM_USD`
- `section_baths`, `section_charges`
- `section_cost_breakdown`, `section_top_circuits` (merged into the usage table)
- `section_today_chart`, `section_week_chart` (replaced by blocks 2 and 3)
- The TOU model in `pi/rates.py`, for reporting purposes

## Phasing

Each phase ships something usable.

0. **Resolve #15 first** — make `energy_wh_counter` authoritative. Prerequisite,
   not a follow-up; see Data source.
1. **Weekly briefing, blocks 1–4**, plus the on-demand test send. No anomaly
   detection, no HVAC block. This is the bulk of the value.
2. **HVAC block** — by-day, WoW, MoM.
3. **Anomaly email** — `report_baseline.py`, both guards, suppression state.
4. **Hot-water vs space-conditioning split** — detector work, separate effort.
   See below.

## Out of scope, tracked separately

**Hot water vs space conditioning.** The panel has one heat pump circuit serving
both domestic hot water and space conditioning — which is why baths are
detectable from its signature at all. The split can never be metered; it must be
disaggregated by behavioural signature. `bath_detector.py` already isolates DHW
events at ≥2500 W mean and ≥0.85 duty cycle; space conditioning is lower, cyclic,
and time-of-day correlated. Adjacent to #14 (outdoor temperature), which is the
same unlock for separating AC from heat. The HVAC block is designed to gain a row
when this lands.

**Making bath and charge events explorable over time.** Requested 2026-08-21.
Their sections leave the email, but the detectors keep writing and the data
deserves a home — most likely the web dashboard rather than a mail. Needs its own
design.
