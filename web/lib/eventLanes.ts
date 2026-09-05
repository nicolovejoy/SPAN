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

/**
 * Place an arbitrary instant on the chart's x axis.
 *
 * lightweight-charts' `timeToCoordinate` only resolves times that are actual
 * points on the series — it returns null anywhere between two buckets. Run and
 * event boundaries are not bucket-aligned (a charge session starts on some
 * second), so asking it directly drops nearly every block once the bucket is
 * coarser than the events themselves. The chart does carry a data or whitespace
 * point at every bucket across the loaded window, so we resolve the two buckets
 * that straddle `ms` and interpolate between them instead.
 *
 * At the edges of the loaded data only one side resolves; we then take the
 * slope from the next bucket inward and extrapolate, falling back to the single
 * resolvable anchor if even that is missing.
 *
 * @param timeToX ms → px, or null when the chart can't place that instant.
 */
export function interpolateX(
  ms: number,
  bucketMs: number,
  timeToX: (ms: number) => number | null,
): number | null {
  const t0 = Math.floor(ms / bucketMs) * bucketMs;
  const t1 = t0 + bucketMs;
  const frac = (ms - t0) / bucketMs;
  const x0 = timeToX(t0);
  const x1 = timeToX(t1);
  if (x0 !== null && x1 !== null) return x0 + (x1 - x0) * frac;
  if (x0 !== null) {
    // Right edge: slope from the bucket before t0.
    const xPrev = timeToX(t0 - bucketMs);
    return xPrev === null ? x0 : x0 + (x0 - xPrev) * frac;
  }
  if (x1 !== null) {
    // Left edge: slope from the bucket after t1.
    const xNext = timeToX(t1 + bucketMs);
    if (xNext === null) return x1;
    const step = xNext - x1;
    return x1 - step + step * frac;
  }
  return null;
}
