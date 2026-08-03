// Slack-window math for the chart. The *loaded* window is deliberately wider
// than the *visible* one: `fixLeftEdge`/`fixRightEdge` bound pan/zoom to the
// loaded data (that's what stops the blank-out), so with loaded === visible a
// drag has nowhere to go. Padding one visible-span on each side gives a full
// preset-width of drag room per side while still never showing empty canvas.
//
// Pure ms math — no chart, no timezone. Callers convert at the chart boundary.

export type Window = { fromMs: number; toMs: number };

/** Padding per side as a multiple of the visible span → loaded ≈ 3× visible. */
export const PAD_FACTOR = 1;

/** Extend the loaded window once the visible edge comes within this fraction
 *  of the visible span of a loaded edge. */
export const EDGE_FRACTION = 0.2;

const floorTo = (ms: number, step: number) => Math.floor(ms / step) * step;

/** Bucket-ms still available for padding once the visible span is paid for.
 *  One interval is held back so bucket-boundary flooring can't push the loaded
 *  window past MAX_BUCKETS. */
function padBudget(
  spanMs: number,
  intervalMs: number,
  maxBuckets: number,
): number {
  return Math.max(0, maxBuckets * intervalMs - spanMs - intervalMs);
}

/**
 * The window to *fetch* for a given visible window: one PAD_FACTOR span of
 * slack per side, clamped at `now` on the right and at the epoch on the left,
 * and shrunk (to zero if need be) so the loaded bucket count stays under
 * `maxBuckets`. The bucket size is chosen from the *visible* span by the
 * caller — padding must never coarsen it.
 */
export function padWindow(
  view: Window,
  opts: {
    nowMs: number;
    intervalMs: number;
    maxBuckets: number;
    padFactor?: number;
  },
): Window {
  const { nowMs, intervalMs, maxBuckets } = opts;
  const padFactor = opts.padFactor ?? PAD_FACTOR;

  const from = Math.min(view.fromMs, view.toMs);
  const to = Math.min(Math.max(view.fromMs, view.toMs), nowMs);
  const span = Math.max(intervalMs, to - from);
  const want = Math.max(0, Math.round(span * padFactor));

  let budget = padBudget(span, intervalMs, maxBuckets);
  // Right first (usually 0 — presets end at `now`), left takes what's left.
  const right = Math.min(want, Math.max(0, nowMs - to), budget);
  budget -= right;
  const left = Math.min(want, Math.max(0, from), budget);

  return {
    fromMs: floorTo(from - left, intervalMs),
    toMs: floorTo(to + right, intervalMs),
  };
}

/**
 * Which side (if any) of the loaded window the view has drifted close enough
 * to that we should load more there. Null when both edges still have room, or
 * when the edge is already hard-stopped (epoch on the left, `now` on the
 * right). Left wins if somehow both are close.
 */
export function needsExtension(
  loaded: Window,
  visible: Window,
  nowMs: number,
  fraction = EDGE_FRACTION,
): "left" | "right" | null {
  const span = Math.max(1, visible.toMs - visible.fromMs);
  const slack = span * fraction;
  if (loaded.fromMs > 0 && visible.fromMs - loaded.fromMs <= slack) return "left";
  if (loaded.toMs < nowMs && loaded.toMs - visible.toMs <= slack) return "right";
  return null;
}

/**
 * Grow the loaded window by up to `stepMs` on one side, same bucket, same
 * MAX_BUCKETS cap. Returns null when there's no room left to grow — the cap
 * (not the gesture) is what eventually stops the walk, and the caller just
 * keeps the window it has.
 */
export function extendWindow(
  loaded: Window,
  side: "left" | "right",
  opts: {
    stepMs: number;
    nowMs: number;
    intervalMs: number;
    maxBuckets: number;
  },
): Window | null {
  const { stepMs, nowMs, intervalMs, maxBuckets } = opts;
  const spanMs = loaded.toMs - loaded.fromMs;
  const budget = padBudget(spanMs, intervalMs, maxBuckets);
  const room =
    side === "left"
      ? Math.max(0, loaded.fromMs)
      : Math.max(0, nowMs - loaded.toMs);
  const grow = Math.min(Math.max(0, stepMs), room, budget);
  if (grow < intervalMs) return null;

  return side === "left"
    ? { fromMs: floorTo(loaded.fromMs - grow, intervalMs), toMs: loaded.toMs }
    : { fromMs: loaded.fromMs, toMs: floorTo(loaded.toMs + grow, intervalMs) };
}
