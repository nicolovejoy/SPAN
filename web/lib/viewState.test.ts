import { describe, it, expect } from "vitest";
import { initView, reducer, type ViewState } from "./viewState";
import { parseState } from "./url-state";

const base = (over: Partial<ViewState> = {}): ViewState => ({
  ...initView(parseState({ range: "24h" })),
  ...over,
});

describe("drill focuses the Show filter", () => {
  it("narrows show to the drilled category and remembers the old one", () => {
    const s = reducer(base({ show: ["HVAC", "Car"] }), {
      type: "drill",
      category: "Lights",
    });
    expect(s.drill).toBe("Lights");
    expect(s.show).toEqual(["Lights"]);
    expect(s.showBeforeDrill).toEqual(["HVAC", "Car"]);
  });

  it("restores the remembered filter when the drill is backed out", () => {
    let s = reducer(base({ show: ["HVAC", "Car"] }), {
      type: "drill",
      category: "Lights",
    });
    s = reducer(s, { type: "drill", category: null });
    expect(s.drill).toBeNull();
    expect(s.show).toEqual(["HVAC", "Car"]);
    expect(s.showBeforeDrill).toBeNull();
  });

  it("remembers 'All' as All, not as the narrowed filter", () => {
    let s = reducer(base({ show: [] }), { type: "drill", category: "Lights" });
    expect(s.show).toEqual(["Lights"]);
    s = reducer(s, { type: "drill", category: null });
    expect(s.show).toEqual([]);
  });

  it("follows the focus to another category, keeping the original memory", () => {
    let s = reducer(base({ show: ["HVAC"] }), {
      type: "drill",
      category: "HVAC",
    });
    s = reducer(s, { type: "drill", category: "Lights" });
    expect(s.show).toEqual(["Lights"]);
    expect(s.showBeforeDrill).toEqual(["HVAC"]);
    s = reducer(s, { type: "drill", category: null });
    expect(s.show).toEqual(["HVAC"]);
  });

  it("still clears the drill when show filters the drilled category out", () => {
    let s = reducer(base({ show: [] }), { type: "drill", category: "Lights" });
    s = reducer(s, { type: "show", show: ["HVAC"] });
    expect(s.drill).toBeNull();
    expect(s.show).toEqual(["HVAC"]);
    // The user picked that filter by hand — there's nothing left to restore.
    expect(s.showBeforeDrill).toBeNull();
  });

  it("keeps the drill (and the memory) when show still includes it", () => {
    let s = reducer(base({ show: [] }), { type: "drill", category: "Lights" });
    s = reducer(s, { type: "show", show: ["Lights", "HVAC"] });
    expect(s.drill).toBe("Lights");
    expect(s.showBeforeDrill).toEqual([]);
  });

  it("survives a range change untouched", () => {
    let s = reducer(base({ show: ["HVAC"] }), {
      type: "drill",
      category: "Lights",
    });
    s = reducer(s, { type: "preset", preset: "7d", now: Date.now() });
    expect(s.drill).toBe("Lights");
    expect(s.show).toEqual(["Lights"]);
    expect(s.showBeforeDrill).toEqual(["HVAC"]);
  });
});

describe("initView", () => {
  it("starts with nothing to restore — a URL drill is already narrowed", () => {
    const s = initView(parseState({ range: "7d", drill: "Lights" }));
    expect(s.show).toEqual(["Lights"]);
    expect(s.showBeforeDrill).toBeNull();
    // Backing out of a drill you arrived at by URL leaves show narrowed.
    expect(reducer(s, { type: "drill", category: null }).show).toEqual([
      "Lights",
    ]);
  });
});
