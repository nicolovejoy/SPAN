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

export const fmtPacificTime = (ms: number): string => timeFmt.format(ms).replace(/ /g, " ");

export function fmtPacificRange(fromMs: number, toMs: number): string {
  if (dayKeyFmt.format(fromMs) === dayKeyFmt.format(toMs)) {
    return `${timeFmt.format(fromMs).replace(/ /g, " ")} – ${timeFmt.format(toMs).replace(/ /g, " ")}`;
  }
  return `${monthDayFmt.format(fromMs)} ${timeFmt.format(fromMs).replace(/ /g, " ")} – ${monthDayFmt.format(toMs)} ${timeFmt.format(toMs).replace(/ /g, " ")}`;
}

export function fmtPacificDayRange(fromMs: number, toMs: number): string {
  const a = monthDayFmt.format(fromMs);
  const b = monthDayFmt.format(toMs);
  return a === b ? a : `${a} – ${b}`;
}
