# Explorer events layer — design

Date: 2026-09-05. Approved from a mock (Claude artifact `f0984690`), synthetic Sep 4 data.
Requested 2026-08-21 ("make bath + charge events explorable over time"), broadened by
`docs/superpowers/notes/2026-09-04-vacation-and-dhw-ground-truth.md` to expose the raw
`hvac_mode` timeline, not just formal baths.

## Goal

Under the power chart, show what the heat pump was doing and when baths and EV charges
happened, aligned to the chart's time axis, plus a list of those events for the visible
window. This is the cross-check tool for "who showered when" against memory, and the way
to see a classifier miss next to the power trace that caused it.

Two pieces ship together. Piece 0 is small and independent; piece 1 is the feature.

## Piece 0 — health-check registry (bounded)

`web/lib/health.ts` hard-codes two checks and `app/api/health/route.ts` repeats them.
Replace with a registry so every Pi service that writes on a cadence is observable.

```ts
export type CheckSpec = {
  name: string;         // collector | backup | weather | hvac_mode
  measurement: string;
  field: string;
  lookback: string;     // Influx range start for queryLastPointTime
  maxAgeSeconds: number;
};
export const HEALTH_CHECKS: CheckSpec[] = [
  { name: "collector", measurement: "circuit",         field: "power_w", lookback: "1h",  maxAgeSeconds: 300 },
  { name: "backup",    measurement: "backup_snapshot", field: "ok",      lookback: "14d", maxAgeSeconds: 30 * 3600 },
  { name: "weather",   measurement: "weather",         field: "temp_f",  lookback: "2d",  maxAgeSeconds: 3 * 3600 },
  { name: "hvac_mode", measurement: "hvac_mode",       field: "mode",    lookback: "2d",  maxAgeSeconds: 45 * 60 },
];
```

Thresholds: weather polls hourly (3× cadence). The classifier loops every 600 s and writes
only completed 5-min intervals, so a healthy newest point is 5–15 min old; 45 min is 3× the
worst healthy case. The route maps the registry with `Promise.all`, and on a query failure
marks every entry failed with the same note, exactly as today. Existing exported constants
`COLLECTOR_MAX_AGE_S` / `BACKUP_MAX_AGE_S` go away; tests in `health.test.ts` move to the
registry shape. Effect: UptimeRobot and the daily health email now alarm on a dead weather
or classifier container. That is the intent.

`bath_event` and `charge_event` are irregular and get no check.

## Piece 1 — events layer

### Data

**Endpoint** `GET /api/events?from=<ms>&to=<ms>` → `{ modes: ModeRun[], events: Event[], modesTruncated: boolean }`.
400 on invalid window (same validation as `/api/energy`).

```ts
type Mode = "heat" | "cool" | "hot_water" | "ambiguous";   // idle is never returned
type ModeRun = {
  mode: Mode; fromMs: number; toMs: number;                  // toMs = last interval start + 5 min
  kwh: number; hpMeanW: number; hpMaxW: number; auxMeanW: number;
};
type Event = {
  kind: "bath" | "charge"; fromMs: number; toMs: number;    // toMs = start + duration_min
  kwh: number; costDollars: number; meanW: number; maxW: number;
  auxActive?: boolean;                                       // bath only
};
```

**Mode runs** come from the `hvac_mode` measurement (fields `mode`, `energy_<mode>_kwh`,
`hp_mean_w`, `hp_max_w`, `aux_mean_w`; one point per 5-min interval, time = interval start).
The Flux query pivots the fields per timestamp. Grouping into runs is a pure function
`groupModeRuns(intervals)` in `web/lib/eventRuns.ts`: consecutive intervals of the same
non-idle mode join if the gap between starts is ≤ 10 min (one missing interval is not a
break; two are). `kwh` sums the mode's energy field; `hpMeanW` is the interval-count-weighted
mean; `hpMaxW` the max. Unit-tested with the fixtures in the Sep 4 note's spirit: a
single-interval run, a one-gap bridge, a two-gap split, mode change mid-stream, idle
dropped.

`hvac_mode` does not store outdoor temperature. The mock's "outdoor 51°F" detail is out of
scope for this round; noted as a follow-up.

**Events** come from `bath_event` and `charge_event` (point time = start; fields
`duration_min`, `energy_kwh`, `cost_dollars`, bath: `hp_mean_power_w`/`hp_max_power_w`/
`aux_active`, charge: `mean_power_w`/`max_power_w`). Query range is
`[from - 24h, to)` so an event that started before the window but overlaps it is included;
events ending before `from` are filtered out server-side.

**Window cap.** Mode runs are only computed for windows ≤ 62 days (a 30-day window is
8,640 intervals; the 90d/1y presets would be 26k–105k). Beyond that `modes` is `[]` and
`modesTruncated` is `true`; `events` always return. The lane shows a muted "zoom in for HP
modes" caption in that state.

**Caching** mirrors `/api/energy`: a server `TtlLru` entry in `lib/queryCache.ts`
(`cachedQueryEvents`, keyed by `from|to`), `Cache-Control` 60 s trailing / 86400 s
historical, and a client `TtlLru` + in-flight dedupe in `lib/clientFetch.ts`
(`fetchEventsCached`). Fetched for the *visible* window, exactly the way the breakdown
table is, from `ExplorerClient`.

### State and URL

`DashState` gains `events: boolean` (default `true`). The intent URL carries `events=0`
only when off; `buildIntentSearch` and the parser in `lib/url-state.ts` handle it, with
tests. The reducer gets a `{ type: "events", on }` action. A new **Events** chip sits at the
right end of the `QuickFilters` row, styled like the category chips, toggling it.

### Lanes

Two 22 px rows directly under the chart, inside `PowerChart` so they can use the chart's own
coordinate mapping (`chart.timeScale().timeToCoordinate(toDisplay(sec))`). Both are
rendered by a new `components/EventLanes.tsx` that receives the current visible window, a
`xOf(ms) → number | null` mapper, the plot width (chart width minus the right price scale,
`chart.priceScale("right").width()`), the mode runs, and the events. It re-renders on the
chart's visible-range subscription that already exists, plus on data change.

- Row 1, gutter label "HP mode": one rect per mode run clipped to the visible window.
  Colours: heat `#f97316`, cool `#38bdf8`, hot water `#a855f7`, ambiguous hatched `#9ca3af`.
  Minimum drawn width 1 px so a 5-min run still shows at 7d.
- Row 2, gutter label "Events": baths as a violet (`#a855f7`) 1.5 px outlined rect,
  charges as a `#3b82f6` rect at 25 % fill with 1 px stroke. A text label ("bath",
  "EV 31.4 kWh") is drawn only when the rect is ≥ 56 px wide.
- Gutter: the chart already has no left gutter, so the two labels sit as absolutely
  positioned 11 px uppercase text at the left edge of each row, over the row background,
  matching the mock. If they collide with a block, the block wins and the label hides
  (label is decoration, data is not).
- Hover: a tooltip (absolutely positioned div, styled like the chart's legend) with
  mode/kind, Pacific start–end, duration, kWh, HP mean/max, aux state; for a hot-water run,
  "contains N bath(s)" computed client-side by overlap. Touch: tap = hover.
- Pure layout maths in `lib/eventLanes.ts`: `clipRuns(runs, fromMs, toMs)`,
  `layoutBlocks(runs, xOf, minPx)`, `labelFits(px)`, `bathsWithin(run, events)`. Unit-tested.
- Focus mode keeps the lanes (they are chart, not chrome). The lanes disappear when
  `events` is off.

### Event list

New `components/EventList.tsx` below the breakdown table, `focus-hide`, only when `events`
is on. Rows are the union of mode runs (including ambiguous) and events for the visible
window, sorted by start. Columns: Kind (swatch + label), Start, End, Duration, kWh, $,
Detail, and a "zoom →" link. Times are Pacific via `Intl.DateTimeFormat` with
`timeZone: "America/Los_Angeles"`; a row whose start and end fall on different Pacific
days shows the date on both. Duration uses the existing `formatDuration`. `$` for baths and
charges is the stored `cost_dollars`; for mode runs it is `costForKwh(kwh)` from
`lib/rates.ts`. Detail: mode runs "HP 2.1 kW mean · contains 1 bath" / "no bath" (hot
water only), baths "HP max 3.4 kW, aux off/on", charges "9.6 kW peak".

Cap: if more than 50 rows, keep the 50 largest by kWh, re-sort by start, and caption
"showing 50 of N by kWh". Header reads "Events · <period label>" using the same
Pacific label helpers the breakdown table uses for its window.

**Zoom** dispatches `{ type: "window", fromMs, toMs, now }` with 10 % padding each side
and a 30-minute minimum window, the same action the overview brush uses, so the chart,
lanes, table and list all follow.

### Errors and empty states

- `/api/events` failure → lanes render empty, list shows "events unavailable" in muted
  text; the chart is unaffected. No retry loop; the next visible-window change refetches.
- Windows before 2026-01-04 (pre-timeline) legitimately return no modes; the lane is just
  empty, no message.
- `modesTruncated` → caption in row 1 as above.

### Testing

Unit (vitest): `eventRuns.test.ts`, `eventLanes.test.ts`, `url-state.test.ts` additions,
`clientFetch` key tests for the new cache, `health.test.ts` registry shape. Chart/React
wiring is manual-verify, per the repo's existing convention. Smoke test on Vercel preview
against the Sep 4 window: hot-water block at midday with a bath outline inside it, an EV
charge in the evening, list rows matching, zoom link working, `events=0` hiding it all,
`/api/health` listing four checks.

## Out of scope (tracked)

- Outdoor temperature in run detail (needs a join against `weather`).
- Drill-down pages and de-cluttering the landing page: #25.
- Shower/laundry predicates, dryer detection: they will appear in the lanes for free once
  they write events, if they follow the `<kind>_event` point shape.

## Files

New: `web/app/api/events/route.ts`, `web/lib/eventRuns.ts` (+test), `web/lib/eventLanes.ts`
(+test), `web/components/EventLanes.tsx`, `web/components/EventList.tsx`.
Changed: `web/lib/influx.ts` (queries), `web/lib/queryCache.ts`, `web/lib/clientFetch.ts`,
`web/lib/url-state.ts` (+test), `web/lib/viewState.ts`, `web/components/QuickFilters.tsx`,
`web/components/PowerChart.tsx`, `web/components/ExplorerClient.tsx`, `web/lib/health.ts`
(+test), `web/app/api/health/route.ts`, `CLAUDE.md`.
