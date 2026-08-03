// Pure math for the overview strip's brush (the draggable window rectangle)
// and the ‹ / › window-step buttons.
//
// The strip renders the whole retained history with pan/zoom disabled, so the
// px↔time mapping is a plain linear map across the drawn extent. Everything
// here is ms + px only — no chart, no DOM, no timezone. The component converts
// pointer coordinates to px offsets and hands them over.

import { INTERVAL_ORDER, isIntervalAllowed, type IntervalKey } from "./interval";
import type { Window } from "./panWindow";

/** Floor on a brushed window. Narrower than this and the rectangle is too small
 *  to grab, and the fetch behind it is pointlessly fine-grained. */
export const MIN_BRUSH_SPAN_MS = 15 * 60_000;

const span = (w: Window) => w.toMs - w.fromMs;

/** ms covered by one horizontal pixel of the strip. */
export function msPerPx(extent: Window, widthPx: number): number {
  return span(extent) / Math.max(1, widthPx);
}

/** Instant → px offset from the strip's left edge (unclamped). */
export function timeToPx(ms: number, extent: Window, widthPx: number): number {
  return (ms - extent.fromMs) / msPerPx(extent, widthPx);
}

/** px offset from the strip's left edge → instant (unclamped). */
export function pxToTime(px: number, extent: Window, widthPx: number): number {
  return extent.fromMs + px * msPerPx(extent, widthPx);
}

/**
 * Slide a window inside the extent without changing its span. A window wider
 * than the extent collapses to the extent itself — dragging can't invent data.
 */
export function clampToExtent(win: Window, extent: Window): Window {
  const w = span(win);
  if (w >= span(extent)) return { ...extent };
  const fromMs = Math.min(Math.max(win.fromMs, extent.fromMs), extent.toMs - w);
  return { fromMs: Math.round(fromMs), toMs: Math.round(fromMs + w) };
}

/** Drag the whole window by `deltaMs`, clamped into the extent. */
export function moveWindow(win: Window, deltaMs: number, extent: Window): Window {
  return clampToExtent(
    { fromMs: win.fromMs + deltaMs, toMs: win.toMs + deltaMs },
    extent,
  );
}

/**
 * Drag one edge. The opposite edge is fixed, so the span changes; the moving
 * edge stops at the extent and can never cross to within `minSpanMs` of its
 * partner (which would make the rectangle ungrabbable).
 */
export function resizeWindow(
  win: Window,
  edge: "left" | "right",
  deltaMs: number,
  extent: Window,
  minSpanMs = MIN_BRUSH_SPAN_MS,
): Window {
  const min = Math.min(minSpanMs, span(extent));
  if (edge === "left") {
    const fromMs = Math.min(
      Math.max(win.fromMs + deltaMs, extent.fromMs),
      win.toMs - min,
    );
    return { fromMs: Math.round(fromMs), toMs: win.toMs };
  }
  const toMs = Math.max(
    Math.min(win.toMs + deltaMs, extent.toMs),
    win.fromMs + min,
  );
  return { fromMs: win.fromMs, toMs: Math.round(toMs) };
}

/** Re-centre the window on `atMs` (a tap somewhere else on the strip). */
export function centerWindow(win: Window, atMs: number, extent: Window): Window {
  const half = span(win) / 2;
  return clampToExtent({ fromMs: atMs - half, toMs: atMs + half }, extent);
}

/**
 * Step back/forward by one window-width. Clamped by the extent, so the right
 * edge stops at `now` (the extent's right edge) — a partial step rather than
 * nothing when there's less than a full width left.
 */
export function stepWindow(
  win: Window,
  dir: -1 | 1,
  extent: Window,
): Window {
  return moveWindow(win, dir * span(win), extent);
}

/** True when the two windows are the same to the ms — used to skip a commit
 *  (and its fetch) when a drag ended where it started. */
export function sameWindow(a: Window, b: Window): boolean {
  return a.fromMs === b.fromMs && a.toMs === b.toMs;
}

/**
 * Bucket for the overview strip: the *finest* interval whose bucket count over
 * the whole history still fits under MAX_BUCKETS. Unlike `autoInterval` (which
 * targets ~175 points for a legible detail chart) the strip wants as much shape
 * as one query can carry. Over ~7 months that lands on 6h today and coarsens on
 * its own as history grows.
 */
export function overviewInterval(fromMs: number, toMs: number): IntervalKey {
  for (const key of INTERVAL_ORDER) {
    if (isIntervalAllowed(key, fromMs, toMs)) return key;
  }
  return INTERVAL_ORDER[INTERVAL_ORDER.length - 1]!;
}
