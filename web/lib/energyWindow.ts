// Previous-window comparison for the breakdown table's Δ column. Pure
// functions so the range math and row-merging are unit-testable without
// touching Influx.

import type { EnergyRow } from "./influx";

/** The immediately-preceding window of equal length: [from-(to-from), from). */
export function previousWindowRange(
  fromMs: number,
  toMs: number,
): { fromMs: number; toMs: number } {
  const span = toMs - fromMs;
  return { fromMs: fromMs - span, toMs: fromMs };
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

/**
 * Combine current-window rows with the previous window's per-category kWh and
 * the window length (needed downstream to prorate the base charge). Additive
 * to EnergyRow — categories present now but absent previously get prevKwh: 0
 * (true "was zero", not "unknown"); categories that vanished are dropped, same
 * as today's behavior for the current window.
 */
export function buildEnergyRows(
  current: EnergyRow[],
  previous: EnergyRow[],
  windowMs: number,
): EnergyRow[] {
  const prevByCategory = new Map(previous.map((r) => [r.category, r.kwh]));
  return current.map((r) => ({
    ...r,
    prevKwh: prevByCategory.get(r.category) ?? 0,
    windowMs,
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

export type Delta =
  | { kind: "percent"; value: number }
  | { kind: "kwh"; value: number }
  | { kind: "none" };

// Below this many kWh the previous window is treated as "effectively zero" —
// a percent change off a near-zero base is meaningless (could be +50000%), so
// fall back to a plain kWh delta, and to "none" when both windows are ~zero.
const NEGLIGIBLE_KWH = 0.05;

/** Pick the more legible delta representation for a category's kWh change. */
export function computeDelta(kwh: number, prevKwh: number | undefined): Delta {
  if (prevKwh === undefined) return { kind: "none" };
  if (prevKwh > NEGLIGIBLE_KWH) {
    return { kind: "percent", value: ((kwh - prevKwh) / prevKwh) * 100 };
  }
  if (kwh > NEGLIGIBLE_KWH) return { kind: "kwh", value: kwh - prevKwh };
  return { kind: "none" };
}
