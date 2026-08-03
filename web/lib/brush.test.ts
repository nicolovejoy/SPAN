import { describe, it, expect } from "vitest";
import {
  MIN_BRUSH_SPAN_MS,
  centerWindow,
  clampToExtent,
  moveWindow,
  msPerPx,
  overviewInterval,
  pxToTime,
  resizeWindow,
  sameWindow,
  stepWindow,
  timeToPx,
} from "./brush";
import { MAX_BUCKETS, intervalSeconds } from "./interval";

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const NOW = Date.UTC(2026, 7, 3, 12, 0, 0);
// ~7 months of history, the real shape of the strip's extent.
const EXTENT = { fromMs: Date.UTC(2026, 0, 1), toMs: NOW };
const WIDTH = 800;

describe("px ↔ time mapping", () => {
  it("maps the extent edges to 0 and the full width", () => {
    expect(timeToPx(EXTENT.fromMs, EXTENT, WIDTH)).toBe(0);
    expect(timeToPx(EXTENT.toMs, EXTENT, WIDTH)).toBe(WIDTH);
  });

  it("round-trips", () => {
    const t = EXTENT.fromMs + 37 * DAY;
    expect(pxToTime(timeToPx(t, EXTENT, WIDTH), EXTENT, WIDTH)).toBeCloseTo(t, 3);
  });

  it("is linear across the strip", () => {
    const mid = pxToTime(WIDTH / 2, EXTENT, WIDTH);
    expect(mid).toBe((EXTENT.fromMs + EXTENT.toMs) / 2);
  });

  it("survives a zero-width strip (pre-measure render)", () => {
    expect(Number.isFinite(msPerPx(EXTENT, 0))).toBe(true);
  });
});

describe("clampToExtent", () => {
  it("leaves an interior window alone", () => {
    const w = { fromMs: NOW - 10 * DAY, toMs: NOW - 9 * DAY };
    expect(clampToExtent(w, EXTENT)).toEqual(w);
  });

  it("shifts (not shrinks) a window hanging off the right edge", () => {
    const w = { fromMs: NOW - 12 * HOUR, toMs: NOW + 12 * HOUR };
    const c = clampToExtent(w, EXTENT);
    expect(c.toMs).toBe(EXTENT.toMs);
    expect(c.toMs - c.fromMs).toBe(DAY);
  });

  it("shifts a window hanging off the left edge", () => {
    const w = { fromMs: EXTENT.fromMs - 5 * DAY, toMs: EXTENT.fromMs - 4 * DAY };
    const c = clampToExtent(w, EXTENT);
    expect(c.fromMs).toBe(EXTENT.fromMs);
    expect(c.toMs - c.fromMs).toBe(DAY);
  });

  it("collapses a window wider than the extent onto the extent", () => {
    const w = { fromMs: EXTENT.fromMs - DAY, toMs: EXTENT.toMs + DAY };
    expect(clampToExtent(w, EXTENT)).toEqual(EXTENT);
  });
});

describe("moveWindow", () => {
  it("drags by the given delta", () => {
    const w = { fromMs: NOW - 10 * DAY, toMs: NOW - 9 * DAY };
    expect(moveWindow(w, -2 * DAY, EXTENT)).toEqual({
      fromMs: NOW - 12 * DAY,
      toMs: NOW - 11 * DAY,
    });
  });

  it("stops at the right edge instead of overshooting", () => {
    const w = { fromMs: NOW - 2 * DAY, toMs: NOW - DAY };
    const m = moveWindow(w, 10 * DAY, EXTENT);
    expect(m.toMs).toBe(EXTENT.toMs);
    expect(m.toMs - m.fromMs).toBe(DAY);
  });
});

describe("resizeWindow", () => {
  const w = { fromMs: NOW - 10 * DAY, toMs: NOW - 5 * DAY };

  it("moves only the dragged edge", () => {
    expect(resizeWindow(w, "left", -DAY, EXTENT)).toEqual({
      fromMs: NOW - 11 * DAY,
      toMs: w.toMs,
    });
    expect(resizeWindow(w, "right", DAY, EXTENT)).toEqual({
      fromMs: w.fromMs,
      toMs: NOW - 4 * DAY,
    });
  });

  it("keeps at least MIN_BRUSH_SPAN_MS between the edges", () => {
    const l = resizeWindow(w, "left", 100 * DAY, EXTENT);
    expect(l.toMs - l.fromMs).toBe(MIN_BRUSH_SPAN_MS);
    const r = resizeWindow(w, "right", -100 * DAY, EXTENT);
    expect(r.toMs - r.fromMs).toBe(MIN_BRUSH_SPAN_MS);
  });

  it("clamps the dragged edge at the extent", () => {
    expect(resizeWindow(w, "left", -400 * DAY, EXTENT).fromMs).toBe(EXTENT.fromMs);
    expect(resizeWindow(w, "right", 400 * DAY, EXTENT).toMs).toBe(EXTENT.toMs);
  });
});

describe("centerWindow", () => {
  it("centres the same span on the tapped instant", () => {
    const w = { fromMs: NOW - 10 * DAY, toMs: NOW - 8 * DAY };
    const at = EXTENT.fromMs + 60 * DAY;
    const c = centerWindow(w, at, EXTENT);
    expect((c.fromMs + c.toMs) / 2).toBe(at);
    expect(c.toMs - c.fromMs).toBe(2 * DAY);
  });
});

describe("stepWindow", () => {
  it("steps back a full window width", () => {
    const w = { fromMs: NOW - 8 * DAY, toMs: NOW - DAY };
    expect(stepWindow(w, -1, EXTENT)).toEqual({
      fromMs: NOW - 15 * DAY,
      toMs: NOW - 8 * DAY,
    });
  });

  it("clamps the forward step at now (partial step, not none)", () => {
    const w = { fromMs: NOW - 3 * DAY, toMs: NOW - DAY };
    const s = stepWindow(w, 1, EXTENT);
    expect(s).toEqual({ fromMs: NOW - 2 * DAY, toMs: NOW });
  });

  it("is a no-op once the window already ends at now", () => {
    const w = { fromMs: NOW - DAY, toMs: NOW };
    expect(sameWindow(stepWindow(w, 1, EXTENT), w)).toBe(true);
  });
});

describe("overviewInterval", () => {
  it("picks the finest bucket that fits under MAX_BUCKETS", () => {
    const iv = overviewInterval(EXTENT.fromMs, EXTENT.toMs);
    const buckets = (EXTENT.toMs - EXTENT.fromMs) / 1000 / intervalSeconds(iv);
    expect(buckets).toBeLessThanOrEqual(MAX_BUCKETS);
    // 7 months → 6h today; it coarsens on its own as history grows.
    expect(iv).toBe("6h");
  });

  it("stays fine for a short history", () => {
    expect(overviewInterval(NOW - 3 * HOUR, NOW)).toBe("1m");
  });

  it("never exceeds the coarsest interval", () => {
    expect(overviewInterval(NOW - 50 * 365 * DAY, NOW)).toBe("1w");
  });
});
