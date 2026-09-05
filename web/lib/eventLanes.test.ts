import { describe, expect, it } from "vitest";
import { affineXOf, labelFits, layoutBlocks, resolveAnchors, type XOf } from "./eventLanes";

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

describe("affineXOf", () => {
  const a = { ms: 1000, x: 100 };
  const b = { ms: 2000, x: 300 };

  it("is exact at both anchors", () => {
    const f = affineXOf(a, b)!;
    expect(f(1000)).toBe(100);
    expect(f(2000)).toBe(300);
  });
  it("is linear in between", () => {
    const f = affineXOf(a, b)!;
    expect(f(1500)).toBe(200);
    expect(f(1250)).toBe(150);
  });
  it("extrapolates beyond both anchors", () => {
    const f = affineXOf(a, b)!;
    expect(f(500)).toBe(0);
    expect(f(2500)).toBe(400);
  });
  it("returns null on coincident anchors", () => {
    expect(affineXOf(a, { ms: 1000, x: 180 })).toBeNull();
  });
});

describe("resolveAnchors", () => {
  // 100ms buckets at 0.1 px/ms. `only` restricts which bucket stamps the chart
  // will resolve, standing in for holes and edges of the loaded data.
  const BUCKET = 100;
  const mapper =
    (only: (ms: number) => boolean) =>
    (ms: number): number | null =>
      only(ms) ? ms / 10 : null;
  const all = mapper(() => true);

  it("takes the containing buckets when both resolve immediately", () => {
    expect(resolveAnchors(1050, 1950, BUCKET, all)).toEqual([
      { ms: 1000, x: 100 },
      { ms: 1900, x: 190 },
    ]);
  });
  it("steps the left anchor inward across a gap", () => {
    const [l, r] = resolveAnchors(1050, 1950, BUCKET, mapper((ms) => ms >= 1300))!;
    expect(l).toEqual({ ms: 1300, x: 130 });
    expect(r).toEqual({ ms: 1900, x: 190 });
  });
  it("steps the right anchor inward across a gap", () => {
    const [l, r] = resolveAnchors(1050, 1950, BUCKET, mapper((ms) => ms <= 1600))!;
    expect(l).toEqual({ ms: 1000, x: 100 });
    expect(r).toEqual({ ms: 1600, x: 160 });
  });
  it("gives up after maxSteps", () => {
    // Nothing resolves until 1900, which is 9 steps from bucket 1000.
    expect(resolveAnchors(1050, 2950, BUCKET, mapper((ms) => ms >= 1900), 8)).toBeNull();
  });
  it("rejects coincident anchors", () => {
    expect(resolveAnchors(1050, 1150, BUCKET, mapper((ms) => ms === 1100))).toBeNull();
  });
});

describe("labelFits", () => {
  it("needs 56px", () => {
    expect(labelFits(55)).toBe(false);
    expect(labelFits(56)).toBe(true);
  });
});
