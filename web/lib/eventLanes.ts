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
