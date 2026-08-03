import { describe, it, expect } from "vitest";
import {
  padWindow,
  needsExtension,
  extendWindow,
  PAD_FACTOR,
} from "./panWindow";
import { MAX_BUCKETS } from "./interval";

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const NOW = Date.UTC(2026, 6, 1, 12, 0, 0);

const buckets = (w: { fromMs: number; toMs: number }, intervalMs: number) =>
  (w.toMs - w.fromMs) / intervalMs;

describe("padWindow", () => {
  it("pads a trailing preset only on the left (right is pinned at now)", () => {
    const view = { fromMs: NOW - DAY, toMs: NOW };
    const w = padWindow(view, {
      nowMs: NOW,
      intervalMs: 15 * MIN,
      maxBuckets: MAX_BUCKETS,
    });
    expect(w.toMs).toBe(NOW);
    expect(view.fromMs - w.fromMs).toBe(DAY * PAD_FACTOR);
  });

  it("pads both sides for a historical window", () => {
    const view = { fromMs: NOW - 3 * DAY, toMs: NOW - 2 * DAY };
    const w = padWindow(view, {
      nowMs: NOW,
      intervalMs: 15 * MIN,
      maxBuckets: MAX_BUCKETS,
    });
    expect(view.fromMs - w.fromMs).toBe(DAY);
    expect(w.toMs - view.toMs).toBe(DAY);
  });

  it("clamps the right pad at now", () => {
    const view = { fromMs: NOW - 2 * DAY, toMs: NOW - 6 * HOUR };
    const w = padWindow(view, {
      nowMs: NOW,
      intervalMs: 15 * MIN,
      maxBuckets: MAX_BUCKETS,
    });
    expect(w.toMs).toBe(NOW);
  });

  it("never returns a window ending after now", () => {
    const view = { fromMs: NOW - HOUR, toMs: NOW + 5 * HOUR };
    const w = padWindow(view, {
      nowMs: NOW,
      intervalMs: MIN,
      maxBuckets: MAX_BUCKETS,
    });
    expect(w.toMs).toBeLessThanOrEqual(NOW);
  });

  it("shrinks the padding rather than exceeding MAX_BUCKETS", () => {
    // 6h at a manually-selected 1m bucket = 360 buckets; 3x would be 1080.
    const view = { fromMs: NOW - 6 * HOUR, toMs: NOW };
    const w = padWindow(view, {
      nowMs: NOW,
      intervalMs: MIN,
      maxBuckets: MAX_BUCKETS,
    });
    expect(buckets(w, MIN)).toBeLessThanOrEqual(MAX_BUCKETS);
    // still padded, just not the full preset width
    expect(w.fromMs).toBeLessThan(view.fromMs);
    expect(w.toMs).toBe(NOW);
  });

  it("drops the padding to zero when the visible span alone fills the cap", () => {
    const view = { fromMs: NOW - MAX_BUCKETS * MIN, toMs: NOW };
    const w = padWindow(view, {
      nowMs: NOW,
      intervalMs: MIN,
      maxBuckets: MAX_BUCKETS,
    });
    expect(w.fromMs).toBe(view.fromMs);
    expect(w.toMs).toBe(view.toMs);
  });

  it("aligns both edges to bucket boundaries", () => {
    const iv = 15 * MIN;
    const view = { fromMs: NOW - DAY - 1234, toMs: NOW - 4321 };
    const w = padWindow(view, {
      nowMs: NOW,
      intervalMs: iv,
      maxBuckets: MAX_BUCKETS,
    });
    expect(w.fromMs % iv).toBe(0);
    expect(w.toMs % iv).toBe(0);
  });
});

describe("needsExtension", () => {
  const loaded = { fromMs: NOW - 3 * DAY, toMs: NOW - DAY };

  it("is null while the view sits in the middle of the loaded window", () => {
    const visible = { fromMs: NOW - 2.5 * DAY, toMs: NOW - 1.5 * DAY };
    expect(needsExtension(loaded, visible, NOW)).toBeNull();
  });

  it("asks for the left when the view nears the left edge", () => {
    const visible = { fromMs: NOW - 2.9 * DAY, toMs: NOW - 1.9 * DAY };
    expect(needsExtension(loaded, visible, NOW)).toBe("left");
  });

  it("asks for the right when the view nears the right edge", () => {
    const visible = { fromMs: NOW - 2.1 * DAY, toMs: NOW - 1.1 * DAY };
    expect(needsExtension(loaded, visible, NOW)).toBe("right");
  });

  it("does not ask for the right when the loaded window already ends at now", () => {
    const trailing = { fromMs: NOW - 2 * DAY, toMs: NOW };
    const visible = { fromMs: NOW - DAY, toMs: NOW };
    expect(needsExtension(trailing, visible, NOW)).toBeNull();
  });

  it("scales the threshold to the visible span, not the loaded span", () => {
    // Zoomed in hard near the left edge: 1h view, 20% = 12min of slack.
    const visible = { fromMs: loaded.fromMs + 30 * MIN, toMs: loaded.fromMs + 90 * MIN };
    expect(needsExtension(loaded, visible, NOW)).toBeNull();
    const closer = { fromMs: loaded.fromMs + 5 * MIN, toMs: loaded.fromMs + 65 * MIN };
    expect(needsExtension(loaded, closer, NOW)).toBe("left");
  });
});

describe("extendWindow", () => {
  const opts = {
    stepMs: DAY,
    nowMs: NOW,
    intervalMs: 15 * MIN,
    maxBuckets: MAX_BUCKETS,
  };

  it("grows the left edge and leaves the right alone", () => {
    const loaded = { fromMs: NOW - 3 * DAY, toMs: NOW - DAY };
    const w = extendWindow(loaded, "left", opts)!;
    expect(w.toMs).toBe(loaded.toMs);
    expect(loaded.fromMs - w.fromMs).toBe(DAY);
  });

  it("grows the right edge, clamped at now", () => {
    const loaded = { fromMs: NOW - 3 * DAY, toMs: NOW - 6 * HOUR };
    const w = extendWindow(loaded, "right", opts)!;
    expect(w.fromMs).toBe(loaded.fromMs);
    expect(w.toMs).toBe(NOW);
  });

  it("returns null when the right edge is already at now", () => {
    const loaded = { fromMs: NOW - 3 * DAY, toMs: NOW };
    expect(extendWindow(loaded, "right", opts)).toBeNull();
  });

  it("returns null once MAX_BUCKETS leaves no room", () => {
    const toMs = NOW - DAY;
    const loaded = { fromMs: toMs - MAX_BUCKETS * 15 * MIN, toMs };
    expect(extendWindow(loaded, "left", opts)).toBeNull();
  });

  it("grows only partway when the cap allows less than a full step", () => {
    const iv = 15 * MIN;
    const capMs = MAX_BUCKETS * iv;
    const loaded = { fromMs: NOW - (capMs - 6 * HOUR), toMs: NOW - DAY };
    const w = extendWindow(loaded, "left", opts)!;
    expect(w.fromMs).toBeLessThan(loaded.fromMs);
    expect((w.toMs - w.fromMs) / iv).toBeLessThanOrEqual(MAX_BUCKETS);
  });
});
