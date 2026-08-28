// Calendar-pace comparison for the breakdown table's Δ column. Pure
// functions so the range math and row-merging are unit-testable without
// touching Influx.

import type { EnergyRow } from "./influx";

const TZ = "America/Los_Angeles";

export type ComparisonGrain = "day" | "week" | "month" | "year";

const DAY_MS = 24 * 60 * 60 * 1000;

/** Calendar grain nearest the viewed window length — what "the period you're
 *  looking at" rounds to in human terms. */
export function comparisonGrain(windowMs: number): ComparisonGrain {
  if (windowMs <= 2 * DAY_MS) return "day";
  if (windowMs <= 14 * DAY_MS) return "week";
  if (windowMs <= 62 * DAY_MS) return "month";
  return "year";
}

export function comparisonLabel(grain: ComparisonGrain): string {
  return {
    day: "vs yesterday",
    week: "vs last week",
    month: "vs last month",
    year: "vs last year",
  }[grain];
}

const wallParts = new Intl.DateTimeFormat("en-CA", {
  timeZone: TZ,
  year: "numeric",
  month: "numeric",
  day: "numeric",
  hour: "numeric",
  minute: "numeric",
  second: "numeric",
  weekday: "short",
  hour12: false,
});

/** Pacific wall-clock at `ms`, as calendar fields plus Monday-based weekday. */
function pacificWall(ms: number) {
  const p: Record<string, string> = {};
  for (const { type, value } of wallParts.formatToParts(ms)) p[type] = value;
  const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  return {
    y: Number(p.year),
    m: Number(p.month),
    d: Number(p.day),
    // "24" appears for midnight under hourCycle h24 quirks; normalize.
    h: Number(p.hour) % 24,
    min: Number(p.minute),
    s: Number(p.second),
    dow: weekdays.indexOf(p.weekday), // 0 = Monday
  };
}

/** UTC ms of Pacific midnight on calendar date (y, m, d). m is 1-based; d may
 *  be out of range (0, -3, 32…) — Date.UTC rolls it over, which is how the
 *  callers step to "yesterday" / "1st of last month" without date libraries. */
function pacificMidnightUtc(y: number, m: number, d: number): number {
  const target = Date.UTC(y, m - 1, d);
  // Guess UTC midnight, then correct by the zone offset observed at the guess;
  // twice, to converge when the correction crosses a DST transition.
  let ts = target;
  for (let i = 0; i < 2; i++) {
    const w = pacificWall(ts);
    ts += target - Date.UTC(w.y, w.m - 1, w.d, w.h, w.min, w.s);
  }
  return ts;
}

/**
 * Calendar-pace comparison windows for the Δ column: the period containing
 * `anchorMs` from its Pacific-calendar start through the anchor, and the same
 * elapsed span of the prior period. The prior span is clamped to that period's
 * own end so a 30-day March never reads February plus a bit of March.
 */
export function paceRanges(
  anchorMs: number,
  grain: ComparisonGrain,
): {
  current: { fromMs: number; toMs: number };
  previous: { fromMs: number; toMs: number };
} {
  const w = pacificWall(anchorMs);
  let curStart: number;
  let prevStart: number;
  let prevPeriodEnd: number;
  switch (grain) {
    case "day":
      curStart = pacificMidnightUtc(w.y, w.m, w.d);
      prevStart = pacificMidnightUtc(w.y, w.m, w.d - 1);
      prevPeriodEnd = curStart;
      break;
    case "week":
      curStart = pacificMidnightUtc(w.y, w.m, w.d - w.dow);
      prevStart = pacificMidnightUtc(w.y, w.m, w.d - w.dow - 7);
      prevPeriodEnd = curStart;
      break;
    case "month":
      curStart = pacificMidnightUtc(w.y, w.m, 1);
      prevStart = pacificMidnightUtc(w.y, w.m - 1, 1);
      prevPeriodEnd = curStart;
      break;
    case "year":
      curStart = pacificMidnightUtc(w.y, 1, 1);
      prevStart = pacificMidnightUtc(w.y - 1, 1, 1);
      prevPeriodEnd = curStart;
      break;
  }
  const elapsed = anchorMs - curStart;
  return {
    current: { fromMs: curStart, toMs: anchorMs },
    previous: {
      fromMs: prevStart,
      toMs: Math.min(prevStart + elapsed, prevPeriodEnd),
    },
  };
}

/**
 * Panel total minus circuit total — the energy the panel meters but no named
 * circuit does (the Square D overflow subpanel, plus any metering slop; see
 * #17). Floored at zero: circuit-level counter totals can occasionally exceed
 * a noisy panel integral over a short window, and a negative "unmonitored"
 * number is never meaningful. Mirrors pi/daily_report.py's
 * unmonitored_week_kwh so the email and the dashboard agree on the method.
 */
export function unmonitoredKwh(panelKwh: number, circuitKwh: number): number {
  return Math.max(0, panelKwh - circuitKwh);
}

/** Human-readable span for a table header — largest unit plus at most one
 *  sub-unit, sub-unit dropped when it rounds to zero. No seconds, no
 *  decimals: `45m`, `6h`, `1h 30m`, `36h`, `10d`, `3d 10h`. */
export function formatDuration(ms: number): string {
  const totalMinutes = Math.round(ms / 60_000);
  if (ms < 60 * 60 * 1000) return `${totalMinutes}m`;
  if (ms < 48 * 60 * 60 * 1000) {
    const h = Math.floor(totalMinutes / 60);
    const m = totalMinutes % 60;
    return m === 0 ? `${h}h` : `${h}h ${m}m`;
  }
  const totalHours = Math.round(ms / (60 * 60 * 1000));
  const d = Math.floor(totalHours / 24);
  const h = totalHours % 24;
  return h === 0 ? `${d}d` : `${d}d ${h}h`;
}

/**
 * Combine viewed-window rows with the calendar-pace comparison values (current
 * period-to-date and the prior period's matching span — see paceRanges) and
 * the window length (needed downstream to prorate the base charge). Additive
 * to EnergyRow — categories present in the window but absent from a pace query
 * get 0 (true "was zero", not "unknown"); categories that vanished are
 * dropped, same as today's behavior for the current window.
 */
export function buildEnergyRows(
  current: EnergyRow[],
  period: EnergyRow[],
  prevPeriod: EnergyRow[],
  windowMs: number,
  periodMs?: number,
): EnergyRow[] {
  const periodByCategory = new Map(period.map((r) => [r.category, r.kwh]));
  const prevByCategory = new Map(prevPeriod.map((r) => [r.category, r.kwh]));
  return current.map((r) => ({
    ...r,
    periodKwh: periodByCategory.get(r.category) ?? 0,
    prevPeriodKwh: prevByCategory.get(r.category) ?? 0,
    windowMs,
    periodMs,
  }));
}

/**
 * Splice a drilled category's circuit rows in directly after that category's
 * row, so the table can render them as indented children of the subtotal (#12).
 * Circuits are ordered by name — the same stable order the chart assigns shades
 * in, so a row's color and its legend entry always agree.
 *
 * A no-op when the category isn't in `rows` (e.g. filtered out, or no data),
 * which is what makes it safe to call unconditionally.
 */
export function mergeDrillRows(
  rows: EnergyRow[],
  circuitRows: EnergyRow[],
  drill: string | null,
): EnergyRow[] {
  if (!drill || circuitRows.length === 0) return rows;
  const children = [...circuitRows].sort((a, b) =>
    a.category.localeCompare(b.category),
  );
  return rows.flatMap((r) =>
    r.category === drill && !r.parent ? [r, ...children] : [r],
  );
}

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

export type Delta =
  | { kind: "delta"; kwh: number; percent?: number }
  | { kind: "none" };

// Below this many kWh a period is treated as "effectively zero".
const NEGLIGIBLE_KWH = 0.05;
// A percent needs a base at least this big to be honest — off a smaller base
// it screams (+137% on 1.3 kWh) while meaning almost nothing.
const PERCENT_MIN_BASE_KWH = 1;

/** Absolute-first delta between the current and prior calendar periods, with
 *  a percent only when the prior period is big enough to make one honest. */
export function computeDelta(
  periodKwh: number | undefined,
  prevPeriodKwh: number | undefined,
): Delta {
  if (periodKwh === undefined || prevPeriodKwh === undefined) {
    return { kind: "none" };
  }
  if (periodKwh <= NEGLIGIBLE_KWH && prevPeriodKwh <= NEGLIGIBLE_KWH) {
    return { kind: "none" };
  }
  const kwh = periodKwh - prevPeriodKwh;
  if (prevPeriodKwh >= PERCENT_MIN_BASE_KWH) {
    return { kind: "delta", kwh, percent: (kwh / prevPeriodKwh) * 100 };
  }
  return { kind: "delta", kwh };
}
