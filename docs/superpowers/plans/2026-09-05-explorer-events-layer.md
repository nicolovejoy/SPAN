# Explorer Events Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Under the power chart, draw a heat-pump mode strip and a bath/EV event row aligned to the chart's time axis, list those events for the visible window with zoom links, and make the weather and hvac-mode services observable in `/api/health`.

**Architecture:** A new `/api/events` route reads `hvac_mode`, `bath_event`, `charge_event` from Influx and returns mode *runs* (consecutive same-mode 5-min intervals grouped server-side by a pure function) plus events. The client fetches it for the visible window through the same TtlLru pattern the breakdown table uses, hands the payload to `PowerChart`, which renders two SVG lanes under the chart using lightweight-charts' own `timeToCoordinate` so they stay aligned during pan/zoom, and to an `EventList` below the breakdown table. An `events` boolean joins `DashState` with an `events=0` intent-URL flag.

**Tech Stack:** Next.js 16 App Router (`web/`), React 19, lightweight-charts 5.2, `@influxdata/influxdb-client`, vitest 4, Tailwind 4. Python side untouched.

**Spec:** `docs/superpowers/specs/2026-09-05-explorer-events-layer-design.md`

## Global Constraints

- All work is under `web/`. Run tests with `cd web && npm test`. Type-check with `cd web && npx tsc --noEmit`.
- Read `web/AGENTS.md`: this Next.js version differs from training data; check `node_modules/next/dist/docs/` before using a Next API you are unsure of.
- Timestamps: UTC at rest, Pacific on display. Never `toISOString().slice(0,10)` for a display date; use `Intl.DateTimeFormat` with `timeZone: "America/Los_Angeles"`.
- Follow the existing file conventions: pure logic in `web/lib/*.ts` with a sibling `*.test.ts`; React in `web/components/*.tsx`, manual-verify only.
- Mode colours: heat `#f97316`, cool `#38bdf8`, hot water `#a855f7`, ambiguous hatched `#9ca3af`; bath outline `#a855f7`; charge `#3b82f6`.
- `hvac_mode` interval length is 5 min; a run joins the next interval when `next.startMs - prev.startMs <= 10 min` (one missing interval bridges, two split).
- Mode runs are computed only for windows ≤ 62 days (`62 * 86400_000` ms); beyond that `modes: []`, `modesTruncated: true`.
- Event list cap: 50 rows, largest by kWh, then re-sorted by start.
- Commit after every task with a message in the repo's style (`web: …`), ending with the session's `Co-Authored-By` / `Claude-Session` trailers given in the session context.

---

### Task 1: Health-check registry

**Files:**
- Modify: `web/lib/health.ts`
- Modify: `web/lib/health.test.ts`
- Modify: `web/app/api/health/route.ts`

**Interfaces:**
- Produces: `export type CheckSpec = { name; measurement; field; lookback; maxAgeSeconds }`, `export const HEALTH_CHECKS: CheckSpec[]` (four entries: collector, backup, weather, hvac_mode), `evaluateCheck` unchanged.
- Removes: `COLLECTOR_MAX_AGE_S`, `BACKUP_MAX_AGE_S` (only the route used them).

- [ ] **Step 1: Add the failing registry test** to `web/lib/health.test.ts` (append after the existing describe):

```ts
import { HEALTH_CHECKS } from "./health";

describe("HEALTH_CHECKS registry", () => {
  it("names the four cadence-bound services once each", () => {
    expect(HEALTH_CHECKS.map((c) => c.name)).toEqual([
      "collector",
      "backup",
      "weather",
      "hvac_mode",
    ]);
  });

  it("thresholds are 3x each service's cadence or more", () => {
    const by = Object.fromEntries(HEALTH_CHECKS.map((c) => [c.name, c]));
    expect(by.collector.maxAgeSeconds).toBe(300);        // 30s poll
    expect(by.backup.maxAgeSeconds).toBe(30 * 3600);     // nightly
    expect(by.weather.maxAgeSeconds).toBe(3 * 3600);     // hourly poll
    expect(by.hvac_mode.maxAgeSeconds).toBe(45 * 60);    // 10-min loop, 5-min intervals
  });

  it("every check has a measurement, field and lookback", () => {
    for (const c of HEALTH_CHECKS) {
      expect(c.measurement).toBeTruthy();
      expect(c.field).toBeTruthy();
      expect(c.lookback).toMatch(/^\d+[hd]$/);
    }
  });
});
```

Merge the new `import` into the existing `import { evaluateCheck } from "./health";` line.

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && npx vitest run lib/health.test.ts`
Expected: FAIL, `HEALTH_CHECKS` is not exported.

- [ ] **Step 3: Replace the two constants with the registry** in `web/lib/health.ts`. Delete the `COLLECTOR_MAX_AGE_S` and `BACKUP_MAX_AGE_S` blocks and add, above `evaluateCheck`:

```ts
/** One artifact-age check per Pi service that writes on a cadence. Order is
 *  the order /api/health reports them. Irregular writers (bath_event,
 *  charge_event) get no check — silence is normal for them. */
export type CheckSpec = {
  name: string;
  measurement: string;
  field: string;
  /** Influx range start for the "newest point" query, e.g. "1h", "2d". */
  lookback: string;
  maxAgeSeconds: number;
};

export const HEALTH_CHECKS: CheckSpec[] = [
  // 10× the 30s poll: a dead collector alarms within minutes, one slow poll doesn't flap.
  { name: "collector", measurement: "circuit", field: "power_w", lookback: "1h", maxAgeSeconds: 300 },
  // Nightly at 03:30 + generous grace for a slow run or a late timer.
  { name: "backup", measurement: "backup_snapshot", field: "ok", lookback: "14d", maxAgeSeconds: 30 * 3600 },
  // weather_poller loops hourly.
  { name: "weather", measurement: "weather", field: "temp_f", lookback: "2d", maxAgeSeconds: 3 * 3600 },
  // hvac_classifier loops every 600s and writes only completed 5-min intervals,
  // so a healthy newest point is 5–15 min old; 45 min is 3× the worst healthy case.
  { name: "hvac_mode", measurement: "hvac_mode", field: "mode", lookback: "2d", maxAgeSeconds: 45 * 60 },
];
```

Update the file's header comment to say "Every check alarms on artifact age" and list the four.

- [ ] **Step 4: Rewrite the route** `web/app/api/health/route.ts` to iterate the registry:

```ts
import { NextResponse } from "next/server";
import { queryLastPointTime } from "@/lib/influx";
import { HEALTH_CHECKS, evaluateCheck, type HealthCheck } from "@/lib/health";

export const dynamic = "force-dynamic";

export async function GET() {
  const now = new Date();
  let checks: HealthCheck[];
  try {
    const lastTimes = await Promise.all(
      HEALTH_CHECKS.map((c) => queryLastPointTime(c.measurement, c.field, c.lookback)),
    );
    checks = HEALTH_CHECKS.map((c, i) =>
      evaluateCheck(c.name, lastTimes[i], now, c.maxAgeSeconds),
    );
  } catch (err) {
    const note = `influx query failed: ${err instanceof Error ? err.message : String(err)}`;
    checks = HEALTH_CHECKS.map((c) => ({
      name: c.name,
      ok: false,
      ageSeconds: null,
      maxAgeSeconds: c.maxAgeSeconds,
      note,
    }));
  }
  const ok = checks.every((c) => c.ok);
  return NextResponse.json(
    { ok, checks },
    { status: ok ? 200 : 503, headers: { "Cache-Control": "no-store" } },
  );
}
```

- [ ] **Step 5: Run tests and type-check**

Run: `cd web && npm test && npx tsc --noEmit`
Expected: all green; no remaining references to `COLLECTOR_MAX_AGE_S` / `BACKUP_MAX_AGE_S` (`grep -rn MAX_AGE_S web/lib web/app` returns nothing).

- [ ] **Step 6: Commit**

```bash
git add web/lib/health.ts web/lib/health.test.ts web/app/api/health/route.ts
git commit -m "web: /api/health reads a check registry; adds weather + hvac_mode freshness"
```

---

### Task 2: Pure event model — runs, list rows, formatting

**Files:**
- Create: `web/lib/eventRuns.ts`
- Create: `web/lib/eventRuns.test.ts`

**Interfaces:**
- Produces (all exported from `web/lib/eventRuns.ts`):

```ts
export type Mode = "heat" | "cool" | "hot_water" | "ambiguous";
export type ModeInterval = { startMs: number; mode: string; kwh: number; hpMeanW: number; hpMaxW: number; auxMeanW: number };
export type ModeRun = { mode: Mode; fromMs: number; toMs: number; kwh: number; hpMeanW: number; hpMaxW: number; auxMeanW: number; intervals: number };
export type EventKind = "bath" | "charge";
export type EventItem = { kind: EventKind; fromMs: number; toMs: number; kwh: number; costDollars: number; meanW: number; maxW: number; auxActive?: boolean };
export type EventsPayload = { modes: ModeRun[]; events: EventItem[]; modesTruncated: boolean };
export type ListRow = { id: string; kind: Mode | EventKind; fromMs: number; toMs: number; kwh: number; costDollars: number; detail: string };
export const INTERVAL_MS = 5 * 60_000;
export const RUN_JOIN_MAX_GAP_MS = 2 * INTERVAL_MS;
export const MODES_MAX_WINDOW_MS = 62 * 86_400_000;
export const LIST_CAP = 50;
export const MODE_LABEL: Record<Mode | EventKind, string>;
export function groupModeRuns(intervals: ModeInterval[]): ModeRun[];
export function bathsWithin(run: { fromMs: number; toMs: number }, events: EventItem[]): EventItem[];
export function buildListRows(payload: EventsPayload, costForKwh: (kwh: number) => number): { rows: ListRow[]; total: number };
export function zoomWindow(fromMs: number, toMs: number): { fromMs: number; toMs: number };
export function formatDurationMs(ms: number): string;          // "1h 40m", "0h 30m"
export function fmtPacificTime(ms: number): string;             // "12:00 PM"
export function fmtPacificRange(fromMs: number, toMs: number): string; // "12:00 PM – 1:40 PM" or "Sep 4 11:30 PM – Sep 5 12:20 AM"
export function fmtPacificDayRange(fromMs: number, toMs: number): string; // "Sep 4" or "Sep 4 – Sep 6"
```

- [ ] **Step 1: Write the failing tests** in `web/lib/eventRuns.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import {
  buildListRows,
  bathsWithin,
  fmtPacificDayRange,
  fmtPacificRange,
  fmtPacificTime,
  formatDurationMs,
  groupModeRuns,
  INTERVAL_MS,
  LIST_CAP,
  zoomWindow,
  type EventItem,
  type EventsPayload,
  type ModeInterval,
} from "./eventRuns";

const T0 = Date.UTC(2026, 8, 4, 19, 0); // 2026-09-04 12:00 PDT
const iv = (i: number, mode: string, kwh = 0.2): ModeInterval => ({
  startMs: T0 + i * INTERVAL_MS,
  mode,
  kwh,
  hpMeanW: 2000 + i * 100,
  hpMaxW: 3000,
  auxMeanW: 0,
});

describe("groupModeRuns", () => {
  it("returns a single-interval run with toMs one interval later", () => {
    const [run] = groupModeRuns([iv(0, "heat")]);
    expect(run).toMatchObject({ mode: "heat", fromMs: T0, toMs: T0 + INTERVAL_MS, intervals: 1 });
  });

  it("joins consecutive same-mode intervals and sums kwh", () => {
    const runs = groupModeRuns([iv(0, "hot_water", 0.3), iv(1, "hot_water", 0.4), iv(2, "hot_water", 0.5)]);
    expect(runs).toHaveLength(1);
    expect(runs[0].kwh).toBeCloseTo(1.2);
    expect(runs[0].toMs).toBe(T0 + 3 * INTERVAL_MS);
    expect(runs[0].hpMeanW).toBeCloseTo(2100); // mean of 2000, 2100, 2200
    expect(runs[0].hpMaxW).toBe(3000);
  });

  it("bridges one missing interval but splits on two", () => {
    const one = groupModeRuns([iv(0, "heat"), iv(2, "heat")]);
    expect(one).toHaveLength(1);
    expect(one[0].toMs).toBe(T0 + 3 * INTERVAL_MS);
    const two = groupModeRuns([iv(0, "heat"), iv(3, "heat")]);
    expect(two).toHaveLength(2);
  });

  it("splits on a mode change and drops idle", () => {
    const runs = groupModeRuns([iv(0, "heat"), iv(1, "idle"), iv(2, "hot_water"), iv(3, "ambiguous")]);
    expect(runs.map((r) => r.mode)).toEqual(["heat", "hot_water", "ambiguous"]);
  });

  it("sorts unsorted input by start", () => {
    const runs = groupModeRuns([iv(1, "cool"), iv(0, "cool")]);
    expect(runs).toHaveLength(1);
    expect(runs[0].fromMs).toBe(T0);
  });

  it("ignores unknown mode strings", () => {
    expect(groupModeRuns([iv(0, "defrost")])).toEqual([]);
  });
});

const bath: EventItem = { kind: "bath", fromMs: T0 + 10 * 60_000, toMs: T0 + 40 * 60_000, kwh: 3.9, costDollars: 0.45, meanW: 2500, maxW: 3400, auxActive: false };
const charge: EventItem = { kind: "charge", fromMs: T0 + 6 * 3600_000, toMs: T0 + 9 * 3600_000, kwh: 31.4, costDollars: 3.64, meanW: 8000, maxW: 9600 };

describe("bathsWithin", () => {
  it("returns baths overlapping the run and ignores charges", () => {
    expect(bathsWithin({ fromMs: T0, toMs: T0 + 3600_000 }, [bath, charge])).toEqual([bath]);
  });
  it("excludes a bath that ends before the run starts", () => {
    expect(bathsWithin({ fromMs: T0 + 3600_000, toMs: T0 + 7200_000 }, [bath])).toEqual([]);
  });
});

describe("buildListRows", () => {
  const cost = (kwh: number) => kwh * 0.1;
  const payload: EventsPayload = {
    // Three intervals: the run ends at T0+15m, so it strictly overlaps the bath
    // starting at T0+10m, and the hp mean (2000, 2100, 2200) is exactly 2100 W.
    modes: groupModeRuns([iv(0, "hot_water", 1), iv(1, "hot_water", 1), iv(2, "hot_water", 1)]),
    events: [bath, charge],
    modesTruncated: false,
  };

  it("unions modes and events sorted by start with detail text", () => {
    const { rows, total } = buildListRows(payload, cost);
    expect(total).toBe(3);
    expect(rows.map((r) => r.kind)).toEqual(["hot_water", "bath", "charge"]);
    expect(rows[0].detail).toBe("HP 2.1 kW mean · contains 1 bath");
    expect(rows[0].costDollars).toBeCloseTo(0.3);
    expect(rows[1].detail).toBe("HP max 3.4 kW, aux off");
    expect(rows[1].costDollars).toBe(0.45);
    expect(rows[2].detail).toBe("9.6 kW peak");
  });

  it("uses stored cost for events, computed cost for mode runs", () => {
    const { rows } = buildListRows(payload, cost);
    expect(rows.find((r) => r.kind === "charge")!.costDollars).toBe(3.64);
  });

  it("caps at the 50 largest by kWh, re-sorted by start", () => {
    const many: EventsPayload = {
      // Spaced three intervals apart (15 min between starts) so none join.
      modes: groupModeRuns(Array.from({ length: 120 }, (_, i) => iv(i * 3, "heat", i % 2 ? 0.1 : 1))),
      events: [],
      modesTruncated: false,
    };
    const { rows, total } = buildListRows(many, cost);
    expect(total).toBe(120);
    expect(rows).toHaveLength(LIST_CAP);
    expect(rows.every((r) => r.kwh === 1)).toBe(true);
    expect(rows.map((r) => r.fromMs)).toEqual([...rows.map((r) => r.fromMs)].sort((a, b) => a - b));
  });

  it("gives every row a stable unique id", () => {
    const { rows } = buildListRows(payload, cost);
    expect(new Set(rows.map((r) => r.id)).size).toBe(rows.length);
  });
});

describe("zoomWindow", () => {
  it("pads 10% each side", () => {
    expect(zoomWindow(0, 3600_000)).toEqual({ fromMs: -360_000, toMs: 3_960_000 });
  });
  it("enforces a 30-minute minimum centred on the event", () => {
    const z = zoomWindow(1_000_000, 1_060_000); // 1 min event
    expect(z.toMs - z.fromMs).toBe(30 * 60_000);
    expect((z.fromMs + z.toMs) / 2).toBe(1_030_000);
  });
});

describe("formatting", () => {
  it("formatDurationMs", () => {
    expect(formatDurationMs(100 * 60_000)).toBe("1h 40m");
    expect(formatDurationMs(30 * 60_000)).toBe("0h 30m");
    expect(formatDurationMs(29.6 * 60_000)).toBe("0h 30m");
  });
  it("fmtPacificTime renders Pacific wall clock", () => {
    expect(fmtPacificTime(T0)).toBe("12:00 PM");
  });
  it("fmtPacificRange adds dates only across a Pacific midnight", () => {
    expect(fmtPacificRange(T0, T0 + 100 * 60_000)).toBe("12:00 PM – 1:40 PM");
    const late = Date.UTC(2026, 8, 5, 6, 30); // 11:30 PM PDT Sep 4
    expect(fmtPacificRange(late, late + 50 * 60_000)).toBe("Sep 4 11:30 PM – Sep 5 12:20 AM");
  });
  it("fmtPacificDayRange", () => {
    expect(fmtPacificDayRange(T0, T0 + 3600_000)).toBe("Sep 4");
    expect(fmtPacificDayRange(T0, T0 + 2 * 86_400_000)).toBe("Sep 4 – Sep 6");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd web && npx vitest run lib/eventRuns.test.ts`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement** `web/lib/eventRuns.ts`:

```ts
// Pure model for the explorer events layer (spec: docs/superpowers/specs/
// 2026-09-05-explorer-events-layer-design.md). No I/O, no React: the server
// route groups hvac_mode intervals into runs with it, the client builds the
// list rows and tooltips with it.

export type Mode = "heat" | "cool" | "hot_water" | "ambiguous";
export type EventKind = "bath" | "charge";

const MODES: readonly Mode[] = ["heat", "cool", "hot_water", "ambiguous"];
const isMode = (m: string): m is Mode => (MODES as readonly string[]).includes(m);

export type ModeInterval = {
  startMs: number;
  mode: string;
  kwh: number;
  hpMeanW: number;
  hpMaxW: number;
  auxMeanW: number;
};

export type ModeRun = {
  mode: Mode;
  fromMs: number;
  /** Last interval start + INTERVAL_MS. */
  toMs: number;
  kwh: number;
  /** Interval-count-weighted mean of hp_mean_w. */
  hpMeanW: number;
  hpMaxW: number;
  auxMeanW: number;
  intervals: number;
};

export type EventItem = {
  kind: EventKind;
  fromMs: number;
  toMs: number;
  kwh: number;
  costDollars: number;
  meanW: number;
  maxW: number;
  auxActive?: boolean;
};

export type EventsPayload = {
  modes: ModeRun[];
  events: EventItem[];
  modesTruncated: boolean;
};

export type ListRow = {
  id: string;
  kind: Mode | EventKind;
  fromMs: number;
  toMs: number;
  kwh: number;
  costDollars: number;
  detail: string;
};

export const INTERVAL_MS = 5 * 60_000;
/** One missing interval bridges a run; two split it. */
export const RUN_JOIN_MAX_GAP_MS = 2 * INTERVAL_MS;
/** Beyond this the route returns no mode runs (a year is 105k intervals). */
export const MODES_MAX_WINDOW_MS = 62 * 86_400_000;
export const LIST_CAP = 50;

export const MODE_LABEL: Record<Mode | EventKind, string> = {
  heat: "Heat",
  cool: "Cool",
  hot_water: "Hot water",
  ambiguous: "Ambiguous",
  bath: "Bath",
  charge: "EV charge",
};

export function groupModeRuns(intervals: ModeInterval[]): ModeRun[] {
  const sorted = [...intervals].sort((a, b) => a.startMs - b.startMs);
  const runs: ModeRun[] = [];
  let cur: (ModeRun & { lastStartMs: number; meanSum: number }) | null = null;
  const flush = () => {
    if (!cur) return;
    const { lastStartMs: _l, meanSum: _m, ...run } = cur;
    runs.push({ ...run, hpMeanW: cur.meanSum / cur.intervals });
    cur = null;
  };
  for (const iv of sorted) {
    if (!isMode(iv.mode)) {
      flush();
      continue;
    }
    if (cur && cur.mode === iv.mode && iv.startMs - cur.lastStartMs <= RUN_JOIN_MAX_GAP_MS) {
      cur.toMs = iv.startMs + INTERVAL_MS;
      cur.kwh += iv.kwh;
      cur.meanSum += iv.hpMeanW;
      cur.hpMaxW = Math.max(cur.hpMaxW, iv.hpMaxW);
      cur.auxMeanW = (cur.auxMeanW * cur.intervals + iv.auxMeanW) / (cur.intervals + 1);
      cur.intervals += 1;
      cur.lastStartMs = iv.startMs;
      continue;
    }
    flush();
    cur = {
      mode: iv.mode,
      fromMs: iv.startMs,
      toMs: iv.startMs + INTERVAL_MS,
      kwh: iv.kwh,
      hpMeanW: iv.hpMeanW,
      hpMaxW: iv.hpMaxW,
      auxMeanW: iv.auxMeanW,
      intervals: 1,
      lastStartMs: iv.startMs,
      meanSum: iv.hpMeanW,
    };
  }
  flush();
  return runs;
}

const overlaps = (a: { fromMs: number; toMs: number }, b: { fromMs: number; toMs: number }) =>
  a.fromMs < b.toMs && b.fromMs < a.toMs;

export function bathsWithin(
  run: { fromMs: number; toMs: number },
  events: EventItem[],
): EventItem[] {
  return events.filter((e) => e.kind === "bath" && overlaps(run, e));
}

const kw = (w: number) => `${(w / 1000).toFixed(1)} kW`;

function modeDetail(run: ModeRun, events: EventItem[]): string {
  const base = `HP ${kw(run.hpMeanW)} mean`;
  if (run.mode !== "hot_water") return base;
  const n = bathsWithin(run, events).length;
  return n === 0 ? `${base} · no bath` : `${base} · contains ${n} bath${n === 1 ? "" : "s"}`;
}

function eventDetail(e: EventItem): string {
  return e.kind === "bath"
    ? `HP max ${kw(e.maxW)}, aux ${e.auxActive ? "on" : "off"}`
    : `${kw(e.maxW)} peak`;
}

export function buildListRows(
  payload: EventsPayload,
  costForKwh: (kwh: number) => number,
): { rows: ListRow[]; total: number } {
  const all: ListRow[] = [
    ...payload.modes.map((r) => ({
      id: `${r.mode}:${r.fromMs}`,
      kind: r.mode,
      fromMs: r.fromMs,
      toMs: r.toMs,
      kwh: r.kwh,
      costDollars: costForKwh(r.kwh),
      detail: modeDetail(r, payload.events),
    })),
    ...payload.events.map((e) => ({
      id: `${e.kind}:${e.fromMs}`,
      kind: e.kind,
      fromMs: e.fromMs,
      toMs: e.toMs,
      kwh: e.kwh,
      costDollars: e.costDollars,
      detail: eventDetail(e),
    })),
  ];
  const total = all.length;
  const kept =
    total > LIST_CAP ? [...all].sort((a, b) => b.kwh - a.kwh).slice(0, LIST_CAP) : all;
  return { rows: kept.sort((a, b) => a.fromMs - b.fromMs), total };
}

const ZOOM_PAD = 0.1;
const ZOOM_MIN_MS = 30 * 60_000;

export function zoomWindow(fromMs: number, toMs: number): { fromMs: number; toMs: number } {
  const span = toMs - fromMs;
  let from = fromMs - span * ZOOM_PAD;
  let to = toMs + span * ZOOM_PAD;
  if (to - from < ZOOM_MIN_MS) {
    const mid = (fromMs + toMs) / 2;
    from = mid - ZOOM_MIN_MS / 2;
    to = mid + ZOOM_MIN_MS / 2;
  }
  return { fromMs: Math.round(from), toMs: Math.round(to) };
}

export function formatDurationMs(ms: number): string {
  const totalMin = Math.round(ms / 60_000);
  return `${Math.floor(totalMin / 60)}h ${String(totalMin % 60).padStart(2, "0")}m`;
}

const TZ = "America/Los_Angeles";
const timeFmt = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
const monthDayFmt = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric" });
const dayKeyFmt = new Intl.DateTimeFormat("en-CA", { timeZone: TZ });

export const fmtPacificTime = (ms: number): string => timeFmt.format(ms);

export function fmtPacificRange(fromMs: number, toMs: number): string {
  if (dayKeyFmt.format(fromMs) === dayKeyFmt.format(toMs)) {
    return `${timeFmt.format(fromMs)} – ${timeFmt.format(toMs)}`;
  }
  return `${monthDayFmt.format(fromMs)} ${timeFmt.format(fromMs)} – ${monthDayFmt.format(toMs)} ${timeFmt.format(toMs)}`;
}

export function fmtPacificDayRange(fromMs: number, toMs: number): string {
  const a = monthDayFmt.format(fromMs);
  const b = monthDayFmt.format(toMs);
  return a === b ? a : `${a} – ${b}`;
}
```

Note `formatDurationMs(30 * 60_000)` must render `"0h 30m"` (test expects it); the mock used that form.

- [ ] **Step 4: Run tests**

Run: `cd web && npx vitest run lib/eventRuns.test.ts`
Expected: PASS. If the `fmtPacificTime` expectation fails on a narrow no-break space (`"12:00 PM"`), normalise inside the formatter with `.replace(/ /g, " ")` rather than loosening the test.

- [ ] **Step 5: Commit**

```bash
git add web/lib/eventRuns.ts web/lib/eventRuns.test.ts
git commit -m "web: pure event model — mode runs, list rows, Pacific formatting"
```

---

### Task 3: Influx queries, server cache, `/api/events` route

**Files:**
- Modify: `web/lib/influx.ts` (append two exported functions)
- Modify: `web/lib/queryCache.ts` (append `cachedQueryEvents`)
- Create: `web/app/api/events/route.ts`
- Create: `web/lib/queryCache.events.test.ts`

**Interfaces:**
- Consumes: `groupModeRuns`, `MODES_MAX_WINDOW_MS`, types from Task 2.
- Produces: `queryHvacModeIntervals(fromMs, toMs): Promise<ModeInterval[]>`, `queryEvents(fromMs, toMs): Promise<EventItem[]>` in `influx.ts`; `cachedQueryEvents({ fromMs, toMs }): Promise<EventsPayload>` and `makeEventsKey(fromMs, toMs)` in `queryCache.ts`; `GET /api/events?from&to` → `EventsPayload` under `{ data }`? **No: the route returns the payload at the top level** (`{ modes, events, modesTruncated }`), unlike `/api/energy`'s `{ data }`.

- [ ] **Step 1: Write the failing key test** `web/lib/queryCache.events.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { makeEventsKey } from "./queryCache";

describe("makeEventsKey", () => {
  it("keys by window only", () => {
    expect(makeEventsKey(1000, 2000)).toBe("events|1000|2000");
    expect(makeEventsKey(1000, 2000)).not.toBe(makeEventsKey(1000, 2001));
  });
});
```

Run: `cd web && npx vitest run lib/queryCache.events.test.ts` → FAIL (not exported).

- [ ] **Step 2: Append the queries** to `web/lib/influx.ts` (bottom of file). `hvac_mode` points carry the fields `mode` (string), `energy_heat_kwh` / `energy_cool_kwh` / `energy_hot_water_kwh` / `energy_idle_kwh` / `energy_ambiguous_kwh`, `hp_mean_w`, `hp_max_w`, `aux_mean_w`; one point per 5-min interval at the interval start. Pivot to one row per timestamp:

```ts
import type { EventItem, ModeInterval } from "./eventRuns";

/**
 * Raw 5-min hvac_mode intervals over [fromMs, toMs) (#14 sub-project 2), one
 * row per interval with the mode's own energy field picked out. The caller
 * groups them into runs. Empty before the timeline starts (2026-01-04).
 */
export async function queryHvacModeIntervals(
  fromMs: number,
  toMs: number,
): Promise<ModeInterval[]> {
  const flux = `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(fromMs)}, stop: ${fluxDate(toMs)})
  |> filter(fn: (r) => r._measurement == "hvac_mode")
  |> filter(fn: (r) => r._field == "mode" or r._field =~ /^energy_.*_kwh$/ or r._field == "hp_mean_w" or r._field == "hp_max_w" or r._field == "aux_mean_w")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
`;
  const out: ModeInterval[] = [];
  const queryApi = makeClient().getQueryApi(ORG);
  for await (const row of queryApi.iterateRows(flux)) {
    const o = row.tableMeta.toObject(row.values) as Record<string, unknown>;
    const mode = String(o.mode ?? "");
    out.push({
      startMs: Date.parse(String(o._time)),
      mode,
      kwh: Number(o[`energy_${mode}_kwh`]) || 0,
      hpMeanW: Number(o.hp_mean_w) || 0,
      hpMaxW: Number(o.hp_max_w) || 0,
      auxMeanW: Number(o.aux_mean_w) || 0,
    });
  }
  return out;
}

/**
 * bath_event + charge_event overlapping [fromMs, toMs). Point time is the
 * event start; duration_min gives the end. The range starts 24h early so an
 * event that began before the window but runs into it is included.
 */
export async function queryEvents(fromMs: number, toMs: number): Promise<EventItem[]> {
  const flux = `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(fromMs - 24 * 3600_000)}, stop: ${fluxDate(toMs)})
  |> filter(fn: (r) => r._measurement == "bath_event" or r._measurement == "charge_event")
  |> pivot(rowKey: ["_time", "_measurement"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
`;
  const out: EventItem[] = [];
  const queryApi = makeClient().getQueryApi(ORG);
  for await (const row of queryApi.iterateRows(flux)) {
    const o = row.tableMeta.toObject(row.values) as Record<string, unknown>;
    const startMs = Date.parse(String(o._time));
    const endMs = startMs + (Number(o.duration_min) || 0) * 60_000;
    if (endMs <= fromMs) continue;
    const bath = o._measurement === "bath_event";
    out.push({
      kind: bath ? "bath" : "charge",
      fromMs: startMs,
      toMs: endMs,
      kwh: Number(o.energy_kwh) || 0,
      costDollars: Number(o.cost_dollars) || 0,
      meanW: Number(bath ? o.hp_mean_power_w : o.mean_power_w) || 0,
      maxW: Number(bath ? o.hp_max_power_w : o.max_power_w) || 0,
      ...(bath ? { auxActive: Boolean(o.aux_active) } : {}),
    });
  }
  return out;
}
```

Put the `import type` at the top of the file with the other imports.

- [ ] **Step 3: Append the cache** to `web/lib/queryCache.ts`:

```ts
import { queryEvents, queryHvacModeIntervals } from "./influx";
import { groupModeRuns, MODES_MAX_WINDOW_MS, type EventsPayload } from "./eventRuns";

// Events cache — window-keyed like energy; independent of the display bucket.
export function makeEventsKey(fromMs: number, toMs: number): string {
  return `events|${fromMs}|${toMs}`;
}

const eventsCache = new Map<string, { data: EventsPayload; expiresAt: number }>();
const eventsInflight = new Map<string, Promise<EventsPayload>>();

export async function cachedQueryEvents(opts: {
  fromMs: number;
  toMs: number;
}): Promise<EventsPayload> {
  const key = makeEventsKey(opts.fromMs, opts.toMs);
  const now = Date.now();

  const hit = eventsCache.get(key);
  if (hit && hit.expiresAt > now) {
    eventsCache.delete(key);
    eventsCache.set(key, hit);
    return hit.data;
  }
  const pending = eventsInflight.get(key);
  if (pending) return pending;

  const isTrailing = now - opts.toMs < 2 * 60_000;
  const ttlMs = isTrailing ? 60_000 : 24 * 60 * 60 * 1000;
  const modesTruncated = opts.toMs - opts.fromMs > MODES_MAX_WINDOW_MS;

  const promise = Promise.all([
    modesTruncated ? Promise.resolve([]) : queryHvacModeIntervals(opts.fromMs, opts.toMs),
    queryEvents(opts.fromMs, opts.toMs),
  ])
    .then(([intervals, events]) => {
      const data: EventsPayload = { modes: groupModeRuns(intervals), events, modesTruncated };
      eventsCache.set(key, { data, expiresAt: Date.now() + ttlMs });
      while (eventsCache.size > MAX_ENTRIES) {
        const oldest = eventsCache.keys().next().value;
        if (oldest === undefined) break;
        eventsCache.delete(oldest);
      }
      eventsInflight.delete(key);
      return data;
    })
    .catch((e) => {
      eventsInflight.delete(key);
      throw e;
    });

  eventsInflight.set(key, promise);
  return promise;
}
```

Merge the `./influx` import into the existing import at the top of `queryCache.ts`.

- [ ] **Step 4: Create the route** `web/app/api/events/route.ts`:

```ts
import { NextResponse } from "next/server";
import { cachedQueryEvents } from "@/lib/queryCache";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Mode runs + bath/charge events for a window. Response is the EventsPayload
 *  at the top level: { modes, events, modesTruncated }. */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const fromMs = Number(url.searchParams.get("from"));
  const toMs = Number(url.searchParams.get("to"));
  if (!Number.isFinite(fromMs) || !Number.isFinite(toMs) || fromMs >= toMs) {
    return NextResponse.json(
      { error: "invalid from/to (ms epoch, from < to required)" },
      { status: 400 },
    );
  }
  const data = await cachedQueryEvents({ fromMs, toMs });
  const isTrailing = Date.now() - toMs < 2 * 60_000;
  const maxAge = isTrailing ? 60 : 86400;
  return NextResponse.json(data, {
    headers: { "Cache-Control": `public, max-age=${maxAge}, stale-while-revalidate=3600` },
  });
}
```

- [ ] **Step 5: Test, type-check, and hit the route locally against the real Influx.**

Run: `cd web && npm test && npx tsc --noEmit` → PASS.

Then, with `web/.env.local` present (it holds the Influx credentials; do not read it, just rely on it), run `cd web && npm run dev` in the background and:

```bash
curl -s 'http://localhost:3000/api/events?from=1757012400000&to=1757098800000' | head -c 1500
```

(That window is 2026-09-04 12:00 PM PDT to 2026-09-05 12:00 PM PDT.) Expected: JSON with a non-empty `modes` array whose entries have `mode`, `fromMs`, `toMs`, `kwh`, `hpMeanW`, `hpMaxW`, `auxMeanW`, `intervals`; `modesTruncated: false`. `events` may be empty or not. Also confirm `?from=1&to=0` returns 400 and a 90-day window returns `modesTruncated: true` with `modes: []`. Stop the dev server afterwards. If `.env.local` is absent, say so in the report and skip; do not create one.

- [ ] **Step 6: Commit**

```bash
git add web/lib/influx.ts web/lib/queryCache.ts web/lib/queryCache.events.test.ts web/app/api/events/route.ts
git commit -m "web: /api/events — hvac_mode runs + bath/charge events for a window"
```

---

### Task 4: Client fetch, `events` state, intent URL, Events chip

**Files:**
- Modify: `web/lib/clientFetch.ts`
- Modify: `web/lib/url-state.ts`, `web/lib/url-state.test.ts`
- Modify: `web/lib/viewState.ts`, `web/lib/viewState.test.ts`
- Modify: `web/components/QuickFilters.tsx`

**Interfaces:**
- Consumes: `EventsPayload` from Task 2; `/api/events` from Task 3.
- Produces: `fetchEventsCached(fromMs, toMs): Promise<EventsPayload>` and `eventsCacheKey(fromMs, toMs)` in `clientFetch.ts`; `DashState.events: boolean`; `buildIntentSearch(range, show, drill, events)` emits `events=0` only when `events === false`; `parseState` reads `events` (`"0"` → false, anything else/absent → true); reducer action `{ type: "events"; on: boolean }`; `QuickFilters` gains props `events: boolean; onEvents: (on: boolean) => void`.

- [ ] **Step 1: Failing tests.** Append to `web/lib/url-state.test.ts`:

```ts
describe("events intent flag", () => {
  it("is on by default and absent from the URL", () => {
    expect(parseState({}).events).toBe(true);
    expect(buildIntentSearch("24h", [], null, true)).toBe("range=24h");
  });
  it("round-trips events=0", () => {
    expect(parseState({ events: "0" }).events).toBe(false);
    expect(buildIntentSearch("24h", [], null, false)).toBe("range=24h&events=0");
  });
  it("treats any other value as on", () => {
    expect(parseState({ events: "1" }).events).toBe(true);
    expect(parseState({ events: "no" }).events).toBe(true);
  });
});
```

Append to `web/lib/viewState.test.ts` (look at how that file constructs a base state and reuse its helper; if none, build one from `parseState({})`):

```ts
describe("events action", () => {
  it("toggles the events flag and nothing else", () => {
    const base = initView(parseState({}));
    const off = reducer(base, { type: "events", on: false });
    expect(off.events).toBe(false);
    expect({ ...off, events: true }).toEqual(base);
    expect(reducer(off, { type: "events", on: true }).events).toBe(true);
  });
});
```

Run: `cd web && npx vitest run lib/url-state.test.ts lib/viewState.test.ts` → FAIL (type errors / undefined).

- [ ] **Step 2: State + URL.** In `web/lib/url-state.ts`:
  - Add to `DashState`: `/** Events layer (mode strip + list) visible. Default true; URL carries events=0 only when off. */ events: boolean;`
  - `buildIntentSearch(range, show, drill = null, events = true)`: after the drill part add `if (!events) parts.push("events=0");`
  - In `parseState`, add `events: first(params.events) !== "0",` to the returned object.
  In `web/lib/viewState.ts`: add `| { type: "events"; on: boolean }` to `Action` and `case "events": return { ...s, events: a.on };` to the reducer.

- [ ] **Step 3: Client fetch.** Append to `web/lib/clientFetch.ts`:

```ts
import type { EventsPayload } from "./eventRuns";

const eventsCache = new TtlLru<EventsPayload>(MAX_ENTRIES);
const eventsInflight = new Map<string, Promise<EventsPayload>>();

export const eventsCacheKey = (fromMs: number, toMs: number): string =>
  `events|${fromMs}|${toMs}`;

export async function fetchEventsCached(
  fromMs: number,
  toMs: number,
): Promise<EventsPayload> {
  const key = eventsCacheKey(fromMs, toMs);
  const hit = eventsCache.get(key, Date.now());
  if (hit) return hit;
  const inflight = eventsInflight.get(key);
  if (inflight) return inflight;

  const p = (async () => {
    const url = new URL("/api/events", location.origin);
    url.searchParams.set("from", String(fromMs));
    url.searchParams.set("to", String(toMs));
    const r = await fetch(url.toString());
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = (await r.json()) as EventsPayload;
    const ttl = cacheTtlMs(toMs, 60_000, Date.now());
    eventsCache.set(key, data, Date.now() + ttl);
    return data;
  })();

  eventsInflight.set(key, p);
  try {
    return await p;
  } finally {
    eventsInflight.delete(key);
  }
}
```

Move the `import type` up with the other imports.

- [ ] **Step 4: Events chip.** In `web/components/QuickFilters.tsx` add props `events: boolean; onEvents: (on: boolean) => void;` and render, after the `CHIPS.map(...)` block and before the closing `</div>`:

```tsx
      <span className="mx-1 text-zinc-300 dark:text-zinc-700" aria-hidden>|</span>
      <Chip active={events} onClick={() => onEvents(!events)}>
        Events
      </Chip>
```

Add `title="Heat-pump mode strip + bath/EV events under the chart"` to that `Chip` by extending `Chip`'s props with an optional `title?: string` passed through to the `<button>`.

- [ ] **Step 5: Wire the chip in `ExplorerClient.tsx`** (minimal, the rest of ExplorerClient changes in Task 6): pass `events={view.events}` and `onEvents={(on) => dispatch({ type: "events", on })}` to `QuickFilters`, and change the URL effect to `buildIntentSearch(view.rangePreset, view.show, view.drill, view.events)` with `view.events` added to its deps.

- [ ] **Step 6: Tests + type-check**

Run: `cd web && npm test && npx tsc --noEmit` → PASS. `page.tsx` calls `parseState`, so `initial.events` now exists; nothing else should need touching. If `tsc` reports other `DashState` object literals missing `events` (tests, fixtures), add `events: true` to them.

- [ ] **Step 7: Commit**

```bash
git add web/lib/clientFetch.ts web/lib/url-state.ts web/lib/url-state.test.ts web/lib/viewState.ts web/lib/viewState.test.ts web/components/QuickFilters.tsx web/components/ExplorerClient.tsx
git commit -m "web: events flag in DashState + intent URL, Events chip, client events cache"
```

---

### Task 5: Lane layout maths + `EventLanes` under the chart

**Files:**
- Create: `web/lib/eventLanes.ts`, `web/lib/eventLanes.test.ts`
- Create: `web/components/EventLanes.tsx`
- Modify: `web/components/PowerChart.tsx`

**Interfaces:**
- Consumes: `ModeRun`, `EventItem`, `EventsPayload`, `bathsWithin`, `fmtPacificRange`, `formatDurationMs`, `MODE_LABEL` from Task 2.
- Produces: `web/lib/eventLanes.ts` exports

```ts
export type XOf = (ms: number) => number | null;
export type Block<T> = { x: number; w: number; item: T; clipped: boolean };
export function layoutBlocks<T extends { fromMs: number; toMs: number }>(items: T[], vis: { fromMs: number; toMs: number }, xOf: XOf, minPx?: number): Block<T>[];
export function labelFits(w: number): boolean;   // w >= 56
export const LANE_H = 22;
export const MODE_COLOR: Record<Mode, string>;
export const EVENT_COLOR: Record<EventKind, string>;
```

  `PowerChart` gains props `events: EventsPayload | null; eventsOn: boolean;`.

- [ ] **Step 1: Failing tests** `web/lib/eventLanes.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { labelFits, layoutBlocks, type XOf } from "./eventLanes";

// Linear mapper: 0..1000 ms → 0..1000 px, null outside the loaded range.
const xOf: XOf = (ms) => (ms < 0 || ms > 1000 ? null : ms);
const vis = { fromMs: 100, toMs: 900 };

describe("layoutBlocks", () => {
  it("maps an interior item to x/w", () => {
    const [b] = layoutBlocks([{ fromMs: 200, toMs: 300 }], vis, xOf);
    expect(b).toMatchObject({ x: 200, w: 100, clipped: false });
  });
  it("clips to the visible window and flags it", () => {
    const [b] = layoutBlocks([{ fromMs: 50, toMs: 300 }], vis, xOf);
    expect(b).toMatchObject({ x: 100, w: 200, clipped: true });
  });
  it("drops items entirely outside the window", () => {
    expect(layoutBlocks([{ fromMs: 0, toMs: 50 }, { fromMs: 950, toMs: 990 }], vis, xOf)).toEqual([]);
  });
  it("enforces a minimum pixel width", () => {
    const [b] = layoutBlocks([{ fromMs: 400, toMs: 400.2 }], vis, xOf, 1);
    expect(b.w).toBe(1);
  });
  it("drops an item whose edges the mapper cannot place", () => {
    const [b] = layoutBlocks([{ fromMs: 200, toMs: 300 }], vis, () => null);
    expect(b).toBeUndefined();
  });
});

describe("labelFits", () => {
  it("needs 56px", () => {
    expect(labelFits(55)).toBe(false);
    expect(labelFits(56)).toBe(true);
  });
});
```

Run: `cd web && npx vitest run lib/eventLanes.test.ts` → FAIL.

- [ ] **Step 2: Implement** `web/lib/eventLanes.ts`:

```ts
import type { EventKind, Mode } from "./eventRuns";

/** ms → chart x in px, or null when the chart can't place it (outside the
 *  loaded data). Built from lightweight-charts' timeToCoordinate. */
export type XOf = (ms: number) => number | null;

export type Block<T> = { x: number; w: number; item: T; clipped: boolean };

export const LANE_H = 22;
const LABEL_MIN_PX = 56;

export const MODE_COLOR: Record<Mode, string> = {
  heat: "#f97316",
  cool: "#38bdf8",
  hot_water: "#a855f7",
  ambiguous: "#9ca3af",
};
export const EVENT_COLOR: Record<EventKind, string> = {
  bath: "#a855f7",
  charge: "#3b82f6",
};

export function layoutBlocks<T extends { fromMs: number; toMs: number }>(
  items: T[],
  vis: { fromMs: number; toMs: number },
  xOf: XOf,
  minPx = 1,
): Block<T>[] {
  const out: Block<T>[] = [];
  for (const item of items) {
    if (item.toMs <= vis.fromMs || item.fromMs >= vis.toMs) continue;
    const from = Math.max(item.fromMs, vis.fromMs);
    const to = Math.min(item.toMs, vis.toMs);
    const x1 = xOf(from);
    const x2 = xOf(to);
    if (x1 === null || x2 === null) continue;
    out.push({
      x: x1,
      w: Math.max(minPx, x2 - x1),
      item,
      clipped: from !== item.fromMs || to !== item.toMs,
    });
  }
  return out;
}

export const labelFits = (w: number): boolean => w >= LABEL_MIN_PX;
```

Run the test → PASS.

- [ ] **Step 3: Create** `web/components/EventLanes.tsx`. It is a presentational component: given the payload, a visible window, a mapper and a plot width, it draws two SVG rows and a hover tooltip.

```tsx
"use client";

import { useState } from "react";
import {
  EVENT_COLOR,
  LANE_H,
  MODE_COLOR,
  labelFits,
  layoutBlocks,
  type XOf,
} from "@/lib/eventLanes";
import {
  MODE_LABEL,
  bathsWithin,
  fmtPacificRange,
  formatDurationMs,
  type EventItem,
  type EventsPayload,
  type ModeRun,
} from "@/lib/eventRuns";

type Hover =
  | { kind: "mode"; run: ModeRun; x: number }
  | { kind: "event"; ev: EventItem; x: number }
  | null;

const kw = (w: number) => `${(w / 1000).toFixed(1)} kW`;

export function EventLanes({
  data,
  visible,
  xOf,
  width,
}: {
  data: EventsPayload | null;
  visible: { fromMs: number; toMs: number };
  xOf: XOf;
  /** Plot-area width in px (chart width minus the right price scale). */
  width: number;
}) {
  const [hover, setHover] = useState<Hover>(null);
  const modes = data ? layoutBlocks(data.modes, visible, xOf) : [];
  const events = data ? layoutBlocks(data.events, visible, xOf) : [];

  return (
    <div className="relative select-none" style={{ width }}>
      {/* Row 1: heat-pump mode */}
      <div className="relative border-t border-zinc-200 dark:border-zinc-800" style={{ height: LANE_H }}>
        <Gutter>HP mode</Gutter>
        {data?.modesTruncated && (
          <span className="absolute left-16 top-1 text-[10px] text-zinc-400">zoom in for HP modes</span>
        )}
        <svg width={width} height={LANE_H} className="block">
          <defs>
            <pattern id="lane-hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="2" height="5" fill={MODE_COLOR.ambiguous} />
            </pattern>
          </defs>
          {modes.map((b) => (
            <rect
              key={`${b.item.mode}:${b.item.fromMs}`}
              x={b.x}
              y={4}
              width={b.w}
              height={LANE_H - 8}
              fill={b.item.mode === "ambiguous" ? "url(#lane-hatch)" : MODE_COLOR[b.item.mode]}
              onMouseEnter={() => setHover({ kind: "mode", run: b.item, x: b.x + b.w / 2 })}
              onMouseLeave={() => setHover(null)}
              onClick={() => setHover({ kind: "mode", run: b.item, x: b.x + b.w / 2 })}
            />
          ))}
        </svg>
      </div>

      {/* Row 2: bath + EV events */}
      <div className="relative" style={{ height: LANE_H }}>
        <Gutter>Events</Gutter>
        <svg width={width} height={LANE_H} className="block">
          {events.map((b) => {
            const bath = b.item.kind === "bath";
            const color = EVENT_COLOR[b.item.kind];
            return (
              <g
                key={`${b.item.kind}:${b.item.fromMs}`}
                onMouseEnter={() => setHover({ kind: "event", ev: b.item, x: b.x + b.w / 2 })}
                onMouseLeave={() => setHover(null)}
                onClick={() => setHover({ kind: "event", ev: b.item, x: b.x + b.w / 2 })}
              >
                <rect
                  x={b.x}
                  y={5}
                  width={b.w}
                  height={LANE_H - 10}
                  rx={2}
                  fill={bath ? "none" : color}
                  fillOpacity={bath ? 1 : 0.25}
                  stroke={color}
                  strokeWidth={bath ? 1.5 : 1}
                />
                {labelFits(b.w) && (
                  <text
                    x={b.x + b.w / 2}
                    y={LANE_H / 2 + 3.5}
                    fontSize={9}
                    textAnchor="middle"
                    fill={bath ? color : "currentColor"}
                    className="pointer-events-none"
                  >
                    {bath ? "bath" : `EV ${b.item.kwh.toFixed(1)} kWh`}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>

      {hover && data && (
        <div
          className="pointer-events-none absolute z-10 rounded border border-zinc-300 bg-white/95 px-2 py-1 text-[11px] shadow dark:border-zinc-700 dark:bg-zinc-900/95"
          style={{ left: Math.max(0, Math.min(width - 220, hover.x - 110)), top: -8, transform: "translateY(-100%)", width: 220 }}
        >
          {hover.kind === "mode" ? <ModeTip run={hover.run} events={data.events} /> : <EventTip ev={hover.ev} />}
        </div>
      )}
    </div>
  );
}

function Gutter({ children }: { children: React.ReactNode }) {
  return (
    <span className="pointer-events-none absolute left-1 top-1 z-[1] text-[10px] uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
      {children}
    </span>
  );
}

function ModeTip({ run, events }: { run: ModeRun; events: EventItem[] }) {
  const baths = run.mode === "hot_water" ? bathsWithin(run, events) : [];
  return (
    <>
      <div><b>{MODE_LABEL[run.mode]}</b> · {fmtPacificRange(run.fromMs, run.toMs)}</div>
      <div className="text-zinc-500">
        {formatDurationMs(run.toMs - run.fromMs)} · {run.kwh.toFixed(1)} kWh · HP mean {kw(run.hpMeanW)} · max {kw(run.hpMaxW)} · aux {run.auxMeanW > 50 ? "on" : "off"}
      </div>
      {baths.length > 0 && (
        <div className="text-zinc-500">
          contains {baths.map((b) => `bath ${fmtPacificRange(b.fromMs, b.toMs)}`).join(", ")}
        </div>
      )}
    </>
  );
}

function EventTip({ ev }: { ev: EventItem }) {
  return (
    <>
      <div><b>{MODE_LABEL[ev.kind]}</b> · {fmtPacificRange(ev.fromMs, ev.toMs)}</div>
      <div className="text-zinc-500">
        {formatDurationMs(ev.toMs - ev.fromMs)} · {ev.kwh.toFixed(1)} kWh · ${ev.costDollars.toFixed(2)} · max {kw(ev.maxW)}
        {ev.kind === "bath" ? ` · aux ${ev.auxActive ? "on" : "off"}` : ""}
      </div>
    </>
  );
}
```

- [ ] **Step 4: Wire into `PowerChart.tsx`.**
  1. Props: change the signature to
     ```tsx
     export function PowerChart({ state, onVisibleChange, events, eventsOn }: {
       state: DashState; onVisibleChange: VisibleChange;
       events: EventsPayload | null; eventsOn: boolean;
     })
     ```
     with `import type { EventsPayload } from "@/lib/eventRuns";` and `import { EventLanes } from "./EventLanes";`.
  2. Add a state tick that re-renders the lanes on every scale change: `const [laneTick, setLaneTick] = useState(0);`. Inside the create-once effect, right after `chart.timeScale().subscribeVisibleTimeRangeChange(onRangeChange);` add:
     ```ts
     let raf = 0;
     const onLogical = () => {
       if (raf) return;
       raf = requestAnimationFrame(() => { raf = 0; setLaneTick((t) => t + 1); });
     };
     chart.timeScale().subscribeVisibleLogicalRangeChange(onLogical);
     ```
     and in the cleanup, before `chart.remove()`: `chart.timeScale().unsubscribeVisibleLogicalRangeChange(onLogical); if (raf) cancelAnimationFrame(raf);`.
  3. Compute the lane inputs at render time (below the `legend` array, before `return`):
     ```ts
     // Lanes read the chart's own scale so they stay pinned under the data
     // during a pan; laneTick forces this block to re-run per scale change.
     void laneTick;
     const chart = chartAliveRef.current ? chartRef.current : null;
     const xOf: XOf = (ms) => {
       if (!chart) return null;
       const x = chart.timeScale().timeToCoordinate(toDisplay(ms / 1000));
       return x === null ? null : x;
     };
     const laneWidth = chart ? chart.timeScale().width() : 0;
     const range = chart?.timeScale().getVisibleRange() ?? null;
     const laneVisible = range
       ? { fromMs: fromDisplay(Number(range.from)) * 1000, toMs: fromDisplay(Number(range.to)) * 1000 }
       : { fromMs: state.fromMs, toMs: state.toMs };
     ```
     with `import type { XOf } from "@/lib/eventLanes";`.
  4. In the JSX, directly after the chart container `<div ref={containerRef} … />`, add:
     ```tsx
     {eventsOn && laneWidth > 0 && (
       <EventLanes data={events} visible={laneVisible} xOf={xOf} width={laneWidth} />
     )}
     ```
     Because the legend/kW label/spinner overlays are `absolute` against the outer `relative` div, the lanes must not sit under them: wrap the chart container and the lanes in their own flow so the overlays stay pinned to the chart. Concretely, change the outer `<div className="relative">` to contain `<div className="relative"><div ref={containerRef} …/>{legend overlay}{kW label}{spinner}{error}</div>{lanes}</div>`. Keep the existing overlay markup verbatim, just re-nest it.
  5. `getVisibleRange()` throws before the chart has data; wrap that call in `try { … } catch { null }`.

- [ ] **Step 5: Temporary wiring so it renders.** In `ExplorerClient.tsx`, pass `events={null} eventsOn={view.events}` to `PowerChart` for now (Task 6 replaces `null` with real data). Run `cd web && npm test && npx tsc --noEmit` → PASS.

- [ ] **Step 6: Visual check.** Start `cd web && npm run dev`, open http://localhost:3000, and confirm the two empty lanes appear under the chart with the "HP mode" / "Events" gutter labels, hide when the Events chip is toggled off, and the chart's legend and kW label are still pinned to the chart. Stop the dev server. Note what you saw in the report.

- [ ] **Step 7: Commit**

```bash
git add web/lib/eventLanes.ts web/lib/eventLanes.test.ts web/components/EventLanes.tsx web/components/PowerChart.tsx web/components/ExplorerClient.tsx
git commit -m "web: EventLanes — mode strip + event row under the chart, aligned via timeToCoordinate"
```

---

### Task 6: Event list + ExplorerClient data wiring + zoom

**Files:**
- Create: `web/components/EventList.tsx`
- Modify: `web/components/ExplorerClient.tsx`

**Interfaces:**
- Consumes: `fetchEventsCached` (Task 4), `buildListRows`, `zoomWindow`, `fmtPacificDayRange`, `fmtPacificTime`, `fmtPacificRange`, `formatDurationMs`, `MODE_LABEL`, `LIST_CAP` (Task 2), `MODE_COLOR`, `EVENT_COLOR` (Task 5), `costForKwh` from `@/lib/rates`, `PowerChart` props from Task 5.
- Produces: `EventList({ data, visible, onZoom })`.

- [ ] **Step 1: Create** `web/components/EventList.tsx`:

```tsx
"use client";

import { costForKwh } from "@/lib/rates";
import { EVENT_COLOR, MODE_COLOR } from "@/lib/eventLanes";
import {
  LIST_CAP,
  MODE_LABEL,
  buildListRows,
  fmtPacificDayRange,
  fmtPacificTime,
  formatDurationMs,
  zoomWindow,
  type EventsPayload,
  type ListRow,
} from "@/lib/eventRuns";

const TZ = "America/Los_Angeles";
const dayKey = new Intl.DateTimeFormat("en-CA", { timeZone: TZ });
const monthDay = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric" });

/** "12:00 PM", or "Sep 5 12:20 AM" when the row crosses a Pacific midnight. */
function cell(ms: number, row: ListRow): string {
  const crosses = dayKey.format(row.fromMs) !== dayKey.format(row.toMs);
  return crosses ? `${monthDay.format(ms)} ${fmtPacificTime(ms)}` : fmtPacificTime(ms);
}

function Swatch({ kind }: { kind: ListRow["kind"] }) {
  const style =
    kind === "bath"
      ? { border: `1.5px solid ${EVENT_COLOR.bath}` }
      : kind === "charge"
        ? { background: EVENT_COLOR.charge, opacity: 0.6 }
        : kind === "ambiguous"
          ? { background: `repeating-linear-gradient(45deg, ${MODE_COLOR.ambiguous} 0 2px, transparent 2px 4px)` }
          : { background: MODE_COLOR[kind] };
  return <i className="inline-block h-2 w-2 rounded-sm" style={style} aria-hidden />;
}

export function EventList({
  data,
  error,
  visible,
  onZoom,
}: {
  data: EventsPayload | null;
  error: boolean;
  visible: { fromMs: number; toMs: number };
  onZoom: (fromMs: number, toMs: number) => void;
}) {
  const { rows, total } = data ? buildListRows(data, costForKwh) : { rows: [], total: 0 };
  return (
    <section className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between">
        <h2 className="text-xs uppercase tracking-wide text-zinc-500">
          Events · {fmtPacificDayRange(visible.fromMs, visible.toMs)}
        </h2>
        <span className="text-xs text-zinc-400">
          {total > LIST_CAP ? `showing ${LIST_CAP} of ${total} by kWh` : "Rows follow the visible window. Click a row to zoom the chart to it."}
        </span>
      </div>
      {error ? (
        <p className="text-xs text-zinc-400">events unavailable</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-zinc-400">no events in this window</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm tabular-nums">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-zinc-500">
                <th className="px-2 py-1.5 font-medium">Kind</th>
                <th className="px-2 py-1.5 font-medium">Start</th>
                <th className="px-2 py-1.5 font-medium">End</th>
                <th className="px-2 py-1.5 text-right font-medium">Duration</th>
                <th className="px-2 py-1.5 text-right font-medium">kWh</th>
                <th className="px-2 py-1.5 text-right font-medium">$</th>
                <th className="px-2 py-1.5 font-medium">Detail</th>
                <th className="px-2 py-1.5" />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.id}
                  className="cursor-pointer border-t border-zinc-100 hover:bg-zinc-50 dark:border-zinc-900 dark:hover:bg-zinc-900/60"
                  onClick={() => {
                    const z = zoomWindow(r.fromMs, r.toMs);
                    onZoom(z.fromMs, z.toMs);
                  }}
                >
                  <td className="px-2 py-1.5"><span className="inline-flex items-center gap-1.5"><Swatch kind={r.kind} />{MODE_LABEL[r.kind]}</span></td>
                  <td className="px-2 py-1.5">{cell(r.fromMs, r)}</td>
                  <td className="px-2 py-1.5">{cell(r.toMs, r)}</td>
                  <td className="px-2 py-1.5 text-right">{formatDurationMs(r.toMs - r.fromMs)}</td>
                  <td className="px-2 py-1.5 text-right">{r.kwh.toFixed(1)}</td>
                  <td className="px-2 py-1.5 text-right">{r.costDollars.toFixed(2)}</td>
                  <td className="px-2 py-1.5 text-xs text-zinc-500">{r.detail}</td>
                  <td className="px-2 py-1.5 text-right text-xs text-zinc-400">zoom →</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
```

- [ ] **Step 2: Wire `ExplorerClient.tsx`.**
  1. Imports: `import { EventList } from "./EventList";`, add `fetchEventsCached` to the `@/lib/clientFetch` import, `import type { EventsPayload } from "@/lib/eventRuns";`.
  2. After the drilled-rows effect, add the events fetch, keyed on the visible window and the flag (no fetch while off):
     ```ts
     const [events, setEvents] = useState<EventsPayload | null>(null);
     const [eventsError, setEventsError] = useState(false);
     useEffect(() => {
       if (!view.events) return;
       let cancelled = false;
       fetchEventsCached(visible.fromMs, visible.toMs)
         .then((d) => { if (!cancelled) { setEvents(d); setEventsError(false); } })
         .catch(() => { if (!cancelled) { setEvents(null); setEventsError(true); } });
       return () => { cancelled = true; };
     }, [visible.fromMs, visible.toMs, view.events]);
     ```
  3. Replace the Task 5 placeholder: `<PowerChart state={view} onVisibleChange={onVisibleChange} events={events} eventsOn={view.events} />`.
  4. After the breakdown-table block, add:
     ```tsx
     {view.events && (
       <div className="focus-hide">
         <EventList
           data={events}
           error={eventsError}
           visible={visible}
           onZoom={(fromMs, toMs) => dispatch({ type: "window", fromMs, toMs, now: Date.now() })}
         />
       </div>
     )}
     ```

- [ ] **Step 3: Tests + type-check.** `cd web && npm test && npx tsc --noEmit` → PASS.

- [ ] **Step 4: Visual check against real data.** `cd web && npm run dev`, open http://localhost:3000/?range=7d and pan to Sep 4. Confirm: violet hot-water blocks and orange heat blocks in the mode lane; any bath outlines / EV spans in the event row; hover tooltip on a block; the list below the breakdown table with matching rows; clicking a row zooms the chart, lanes and list to it; toggling the Events chip off removes lanes and list and puts `events=0` in the URL; reload with `events=0` keeps it off. Stop the dev server. Record what you saw, including anything off (alignment drift during a pan is the thing to watch).

- [ ] **Step 5: Commit**

```bash
git add web/components/EventList.tsx web/components/ExplorerClient.tsx
git commit -m "web: EventList under the breakdown table; events fetched per visible window; row click zooms"
```

---

### Task 7: Docs + spec correction

**Files:**
- Modify: `CLAUDE.md` (the `## web/ — power explorer` section and the Next Steps bullet)
- Modify: `docs/superpowers/specs/2026-09-05-explorer-events-layer-design.md`

- [ ] **Step 1: CLAUDE.md.** In the `## web/ — power explorer` bullet list add, after the breakdown-table bullets:

```markdown
- **Events layer (2026-09-05):** two SVG lanes under the chart (heat-pump `hvac_mode` runs; `bath_event` + `charge_event` spans) drawn inside `PowerChart` via lightweight-charts' `timeToCoordinate` so they track pan/zoom, plus an `EventList` under the breakdown table (rows follow the visible window, click zooms). Data from `/api/events?from&to` → `{ modes, events, modesTruncated }`; runs grouped server-side by `lib/eventRuns.ts` (pure, tested), layout in `lib/eventLanes.ts` (pure, tested). Mode runs are skipped beyond 62-day windows. `events=0` in the intent URL hides the layer; on by default. Spec: `docs/superpowers/specs/2026-09-05-explorer-events-layer-design.md`. Future de-clutter / drill-down pages: #25.
```

Update the `/api/health` bullet to say it reads `HEALTH_CHECKS` in `web/lib/health.ts` with four checks: collector ≤300s, backup ≤30h, weather ≤3h, hvac_mode ≤45min.

In Next Steps: delete the "Make bath + charge events explorable over time" bullet (shipped) and, in the `weather_poller.py has no dead-service detection` bullet, replace the sentences claiming neither service is checked by `/api/health` with: "Both are now in `/api/health` (2026-09-05) so a dead container pages via UptimeRobot; the remaining gap is that an outage longer than the self-heal window still needs a manual `--backfill`." Keep the rest of that bullet.

- [ ] **Step 2: Spec correction.** In the spec's "Mode runs" paragraph, replace "join if the gap between starts is ≤ 5 min (one missing interval is not a break; two are)" with "join if the gap between starts is ≤ 10 min (one missing interval is not a break; two are)".

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-09-05-explorer-events-layer-design.md
git commit -m "CLAUDE.md: events layer + health registry shipped; spec run-gap wording"
```

---

## Self-review notes

- Spec coverage: piece 0 → Task 1; data/endpoint/caching → Task 3 + 4; state/URL/chip → Task 4; lanes + tooltip + truncation caption → Task 5; list + cap + zoom + errors → Task 6; docs → Task 7. Tooltip "contains N bath(s)" → `ModeTip`. Touch = tap: `onClick` sets hover.
- Consistency: `EventsPayload` is returned top-level by the route and consumed as such by `fetchEventsCached`. `formatDurationMs` is the only duration formatter (the older `formatDuration` no longer exists in the repo).
- Known simplification: `ModeTip` shows "aux on" when the run's mean aux draw exceeds 50 W; the list row detail for baths uses the stored `aux_active` flag.
