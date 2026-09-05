import { describe, expect, it } from "vitest";
import { interpolateX, labelFits, layoutBlocks, type XOf } from "./eventLanes";

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

describe("interpolateX", () => {
  // 100ms buckets at 0.1 px/ms. `only` restricts which bucket stamps the chart
  // will resolve, standing in for the edges of the loaded data.
  const BUCKET = 100;
  const mapper =
    (only: (ms: number) => boolean) =>
    (ms: number): number | null =>
      only(ms) ? ms / 10 : null;
  const all = mapper(() => true);

  it("lerps between the two surrounding buckets", () => {
    expect(interpolateX(1050, BUCKET, all)).toBe(105);
  });
  it("returns the bucket's own coordinate on an exact boundary", () => {
    expect(interpolateX(1100, BUCKET, all)).toBe(110);
  });
  it("extrapolates off the right edge from the previous bucket", () => {
    // Nothing past 1100 resolves: step back to 1000 for the slope.
    expect(interpolateX(1150, BUCKET, mapper((ms) => ms <= 1100))).toBe(115);
  });
  it("extrapolates off the left edge from the following bucket", () => {
    // Nothing before 1100 resolves: step forward to 1200 for the slope.
    expect(interpolateX(1050, BUCKET, mapper((ms) => ms >= 1100))).toBe(105);
  });
  it("falls back to the one resolvable anchor when the slope is unknowable", () => {
    expect(interpolateX(1150, BUCKET, mapper((ms) => ms === 1100))).toBe(110);
    expect(interpolateX(1050, BUCKET, mapper((ms) => ms === 1100))).toBe(110);
  });
  it("returns null when neither surrounding bucket resolves", () => {
    expect(interpolateX(1050, BUCKET, () => null)).toBeNull();
  });
});

describe("labelFits", () => {
  it("needs 56px", () => {
    expect(labelFits(55)).toBe(false);
    expect(labelFits(56)).toBe(true);
  });
});
