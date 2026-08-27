# HVAC mode classifier — Phase 0 findings (#14 sub-project 2)

**Date:** 2026-08-26
**Data:** 2026-01-04 → 2026-08-26, 72,924 five-minute intervals, 144 historical
`bath_event` points, 5,643 hourly weather readings.
**Method:** ran on the Pi against live InfluxDB, from a scratch copy of the classifier
inside the `pi-collector` image (`/tmp/hvac-phase0`) — the Pi's git checkout was never
moved onto the feature branch and nothing was written. `--backtest` writes nothing;
threshold sweeps re-classified a cached bucketing in memory (bucketing depends only on
`IDLE_POWER_W`, held fixed at 50 W), so 30+ combinations cost one query pass.

## Gate results

| Gate | Target | Result | Verdict |
|---|---|---|---|
| 1. Bath parity | ≥ 95% | best 93.8% (with 63 false positives); best balanced 90.3% / 35 FP | **FAIL** |
| 2. Seasonal sanity | ~0 heat in July, ~0 cool in January | max July heat 0.25 kWh/day; max January cool 0.00 kWh/day | **PASS** |
| 3. Energy conservation | within 2% | **+0.31%** (Jun 1–8: classifier 21.905 kWh vs `circuit_1h` counter 21.837 kWh, 2016/2016 intervals) | **PASS** |
| Ambiguous share | < ~10% of active energy | 7.5–8.8% at candidate configs (10.4% at seed) | **PASS** |

Gates 2, 3 and the ambiguous share pass comfortably and are insensitive to the
thresholds under discussion. **Gate 1 is the only failure, and it is not reachable
under any configuration tested.**

## Why the seed thresholds failed (root cause)

The seeds were carried over from `bath_detector.py`: mean ≥ 2500 W, duty ≥ 0.85.
At those values parity was 63.2% (91 matched / 53 missed).

Probing individual missed baths showed the mechanism. **The heat-pump DHW reheat
ramps.** Two examples:

- `2026-07-13 03:01Z` (July — no space heating to confound it): power climbed
  2020 → 2077 → 2170 → 2257 → 2340 → 2440 → 2542 → 3259 → 3606 W over 45 minutes.
  With the 2500 W gate only the last 15 minutes qualified, the run fell under the
  25-minute bath floor, and the bath was missed.
- `2026-01-13 12:40Z` (January): same clipping at the front, plus the tail lost to
  the 0.85 duty gate (a 0.70-duty interval closing the draw).

This is **not** the spec's designated bail-out condition ("DHW proves inseparable in
winter"). Misses spanned every season including July, and the mechanism is threshold
clipping of a ramp, not heat/DHW confusion. Tuning was the correct response.

## What tuning could and could not fix

Lowering `DHW_MEAN_POWER_MIN_W` to capture the ramp raises recall steeply — and raises
false positives just as steeply. The frontier (240 days, `DHW_DUTY_MIN` 0.65):

| `DHW_MEAN_POWER_MIN_W` | `BATH_MIN_MINUTES` | parity | matched/missed/extra | hot_water kWh |
|---|---|---|---|---|
| 1900 | 25 | 93.8% | 135 / 9 / 63 | 784.4 |
| 2100 | 25 | 91.7% | 132 / 12 / 59 | 713.7 |
| 2300 | 25 | 90.3% | 130 / 14 / 35 | 647.0 |
| 2300 | 30 | 73.6% | 106 / 38 / 6 | 647.0 |
| 2500 (seed) | 25 | 66.7% | 96 / 48 / 3 | 527.8 |

## Two hypotheses tested and falsified

1. **Split baths.** Theory: a mid-draw dip splits one bath into two runs, producing a
   match plus a false positive. Tested by bridging non-`hot_water` gaps of 1–3
   intervals between `hot_water` runs. Effect was negligible (+1–2 matched, +1–4
   extras across the grid). **Rejected** — and this settles the spec's open question
   ("tolerating single-interval dropouts is a Phase 0 decision, default: no
   tolerance"): keep the default, no tolerance.
2. **Duration separates baths from other draws.** Rejected — see below.

## What the false positives actually are

Characterising the 35 extras at `dhwP=2300 / bathMin=25` against the 130 matched:

```
matched: n=130  duration median 40 min (p10 25, p90 55)  median 2.15 kWh  median 2996 W
extra:   n= 35  duration median 25 min (p10 25, p90 30)  median 1.15 kWh  median 2745 W
```

The extras sit against the duration floor (p90 = 30 min) and carry **half the energy**
of a matched bath. Their Pacific hour-of-day distribution peaks at 21:00, the same
evening window as real baths.

These read as **showers**, not baths — a category the plan already anticipates
("shower / laundry hot-water predicates are future one-liners here"). The old detector
excluded them, and was right to.

Duration alone cannot separate the two populations: real baths also start at 25 min, so
the distributions overlap in 25–30 min. An **energy floor** cuts diagonally across
duration × power and separates them better:

| config | parity | matched/missed/extra | F1 |
|---|---|---|---|
| `dhwP=2100, bathMin=25, minKWh=0` | 91.7% | 132 / 12 / 59 | 78.8% |
| `dhwP=2100, bathMin=25, minKWh=1.5` | 83.3% | 120 / 24 / 15 | **86.0%** |
| `dhwP=2300, bathMin=25, minKWh=0` | 90.3% | 130 / 14 / 35 | 84.1% |

Best balanced accuracy is an energy floor around 1.5 kWh, which cuts false positives
from 59 to 15 at the cost of recall.

## The structural finding

**Gate 1 measures agreement with `bath_detector.py`, which is itself a heuristic, not
ground truth.** The plan anticipated this ("the old detector is not ground truth
either — diffs may be its bugs"), and the data bears it out in both directions:

- The old detector's 2500 W bar **clipped the same ramp** the seeds clipped, so some of
  its 144 events are mistimed and some real baths never entered its history at all.
- The new classifier at a permissive gate finds a population of ~25–30 min / ~1.2 kWh
  draws that are probably showers — real hot-water events, correctly `hot_water` in the
  timeline, but wrongly promoted to `bath_event`.

So the 95% target is being asked of a comparison where both sides have known errors.
Chasing it by loosening thresholds would import 35–63 showers into the bath history.

**Crucially, this does not affect the sub-project's actual deliverable.** The
`hvac_mode` timeline, the heat/cool/hot-water energy split, and the web breakdown
depend on gates 2 and 3, which pass with wide margins. Only `bath_event` parity — a
compatibility concern for one section of a weekly email — is unresolved.

## Recommended constants

Two independent knobs, which the plan's single gate conflated:

- **The DHW gate** (`DHW_MEAN_POWER_MIN_W`, `DHW_DUTY_MIN`) sets how much energy is
  attributed to hot water in the web breakdown. Physically the ramp *is* hot water, so
  a lower gate is more accurate: **2100 W / 0.65 duty**, giving 713.7 kWh over the
  period and an 8.0% ambiguous share.
- **The bath predicate** (`BATH_MIN_MINUTES`, `BATH_MEAN_POWER_MIN_W`, and a proposed
  `BATH_MIN_KWH`) sets what becomes a `bath_event`. **25 min / 2400 W / 1.5 kWh**
  gives the best balanced accuracy (83.3% parity, 15 false positives).

`HEAT_MAX_TEMP_F` and `COOL_MIN_TEMP_F` were left at their seeds (58 / 68). The
ambiguous share is already under target and seasonal sanity is emphatic; widening the
bands would trade a passing gate for nothing.

## Open decision

Gate 1 cannot be met. See the ledger for the decision taken and its rationale.
