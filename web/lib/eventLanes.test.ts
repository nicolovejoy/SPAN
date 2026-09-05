import { describe, expect, it } from "vitest";
import { labelFits, layoutBlocks, type XOf } from "./eventLanes";

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

describe("labelFits", () => {
  it("needs 56px", () => {
    expect(labelFits(55)).toBe(false);
    expect(labelFits(56)).toBe(true);
  });
});
