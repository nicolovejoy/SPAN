# Vacation-window detection + two new DHW/heat ground-truth misses

**Date:** 2026-09-04/05
**Trigger:** Nico asked Claude to guess the vacation window (music camp trip) and the
heating system's turn-on date from the last ~10 days of Influx data, then confirmed/
corrected the findings against what actually happened. Ad-hoc analysis, not code —
queries run via a throwaway `tmp/query_usage.sh` (gitignored), read back by Claude.

## Vacation window — confirmed

Cross-referencing whole-house daily kWh, EV charge events, bath events, and kitchen
appliance (dishwasher/cooktop) daily kWh, plus (after the fact) a lights-circuit
baseline:

- **Departed evening of Sunday, Aug 30, 2026.** Last EV charge + last bath event both
  landed the prior evening (Sat Aug 29, ~6:45–7:40pm Pacific); Sunday itself was still
  a normal/heavy usage day (43.94 kWh, dishwasher ran, EV topped off 19.67 kWh) — read
  as last-day-of-prep, not departure day itself.
- **Vacant Mon Aug 31 – Wed Sep 2.** Whole-house total held flat at 12.6–15.0 kWh/day
  (vs. 18–53 kWh/day before), zero EV charging, zero bath events, kitchen appliances
  silent.
- **Returned Thu Sep 3 evening / Fri Sep 4.** Lights (esp. "Lights / Downstairs") broke
  the vacant-period baseline starting ~5pm Thursday — see below, this method turned out
  to be the most reliable one and is the actionable finding here.

## Two new DHW/heat classifier misses (confirmed against real events)

The existing Phase 0 findings
([2026-08-26-hvac-phase0-findings.md](2026-08-26-hvac-phase0-findings.md)) already
established the DHW signature (mean ≥2100W, duty ≥0.65, 10–120min, ≤2 transitions)
clips the front of slow reheat ramps. Two more real, dated examples surfaced this
session:

1. **Fri Sep 4, ~12:10–12:55pm Pacific.** Nico got home, raised the in-floor heat
   setpoint, showered, and ran the washer within the same ~45 minutes. The interval
   shows two humps (1387→1580→1635→672W, gap, then 154→485→494W) — classified
   `ambiguous` throughout, not `heat`. Plausibly correct labeling by accident (midday
   outdoor temp likely sat in the 58–68°F dead zone) but also structurally
   unresolvable: heat, DHW, and the (unmonitored) washer's hot-water draw all hit the
   *same* metered Heat Pump circuit at once. No amount of threshold tuning
   disambiguates three simultaneous causes from one power trace.
2. **Fri night Sep 4, ~10:30pm Pacific.** A friend showered in an upstairs bathroom
   (different from the master). **Zero power deviation for the full 9:30–11:00pm
   window** (flat ~12W baseline) — no DHW signature at all — followed by a slow ramp
   1243W→1719W starting 11:05pm, classified `heat`. Two explanations, unresolved:
   either the reheat was delayed ~35min by tank buffering and the ramping shape (not
   the tuned flat-plateau shape) got misclassified as `heat`, or the upstairs bathroom
   runs on separate water-heating equipment not behind the monitored HP circuit at
   all. **Open question for Nico:** does upstairs have its own water heater?

A third minor pattern: an early-Friday-morning (~4:55–7:30am) stretch alternates
rapidly between `heat` and `hot_water` labels with erratic power (300–5057W) — Nico's
read is this is DHW tank topping-off, not real space heating, given the in-floor
setpoint wasn't actually raised until his noon return. If so, this is a case where one
physical event (a tank recovery) gets sliced into a flickering sequence of different
5-minute labels rather than one clean event.

## Actionable takeaways

1. **Heat, cool, and DHW share one meter, permanently** (Stiebel Eltron is a single
   integrated unit — this is already known, see roadmap.md's "through-line", but now
   has two dated field examples of the failure mode it causes). Overlapping calls are
   not a tuning problem; they need a separate DHW-loop sensor to ever fully resolve.
2. **The DHW signature's shape assumption (flat plateau, 10–120min) has now missed
   twice** — once at the front of a fast ramp (Aug 26 findings) and once on a slow
   ramp with a ~35min delayed onset (this session). Worth folding into a future
   recalibration pass alongside the existing findings doc.
3. **Fixture-level attribution (which bathroom/shower) is not recoverable** from
   current metering — confirmed again by the silent 90-minute window before the
   11:05pm ramp.
4. **Lights-circuit baseline deviation is a much cleaner occupancy/presence signal
   than anything HVAC- or grid-based.** The vacant-period baseline for "Lights /
   Downstairs" was stable to ±2W night over night (39→41W dusk, 78→80W late evening);
   Thursday's visitor broke that baseline by 2–3x starting ~5pm, concentrated in
   living-area circuits (Downstairs, Living room) with bedroom circuits untouched.
   HVAC reacts to weather regardless of occupancy; grid totals blend EV/kitchen/
   everything together. Lights-baseline deviation isolated the signal cleanly.
5. **Departure detects easily, arrival doesn't.** EV charging + bath events + kitchen
   appliance use all going quiet together for 24h+ is a fast, high-confidence
   departure signal (this session called it to within one day, four independent
   corroborating signals). Return builds up gradually over hours and was only cleanly
   caught by the lights-baseline method — HVAC and grid totals lagged/blurred it.
6. **Washer's hot-water draw is invisible today** (unmonitored subpanel, #17 part 2
   not yet built) — this now has a second concrete motivating case beyond the
   original "Unmonitored" breakdown row: disentangling laundry-driven DHW reheats
   from shower-driven ones in the Fri-noon event above.

## Dashboard implications

See CLAUDE.md Next Steps for the two backlog items this informed (presence signal
from lights baseline; broaden the bath/charge explorer to raw `hot_water` intervals).
