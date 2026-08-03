import { describe, it, expect } from "vitest";
import { buildIntentSearch, parseDrill, parseState } from "./url-state";

describe("buildIntentSearch (intent-only URL)", () => {
  it("is empty when there is no preset and no filter", () => {
    expect(buildIntentSearch(null, [])).toBe("");
  });

  it("carries just the preset", () => {
    expect(buildIntentSearch("7d", [])).toBe("range=7d");
  });

  it("carries just the filter, commas intact (not %2C)", () => {
    expect(buildIntentSearch(null, ["HVAC", "Car"])).toBe("show=HVAC,Car");
  });

  it("carries preset and filter together", () => {
    expect(buildIntentSearch("24h", ["HVAC"])).toBe("range=24h&show=HVAC");
  });

  it("omits an empty show list", () => {
    expect(buildIntentSearch("1h", [])).toBe("range=1h");
  });

  it("omits the drill when nothing is drilled", () => {
    expect(buildIntentSearch("7d", ["HVAC"])).toBe("range=7d&show=HVAC");
    expect(buildIntentSearch("7d", ["HVAC"], null)).toBe("range=7d&show=HVAC");
  });

  it("carries the drilled category", () => {
    expect(buildIntentSearch("7d", ["HVAC"], "HVAC")).toBe(
      "range=7d&show=HVAC&drill=HVAC",
    );
    expect(buildIntentSearch(null, [], "Car")).toBe("drill=Car");
  });
});

describe("parseDrill", () => {
  it("accepts a real category", () => {
    expect(parseDrill("HVAC", [])).toBe("HVAC");
    expect(parseDrill("Else", [])).toBe("Else");
  });

  it("rejects anything that isn't a category", () => {
    expect(parseDrill("Furnace", [])).toBeNull();
    expect(parseDrill("", [])).toBeNull();
    expect(parseDrill(undefined, [])).toBeNull();
  });

  it("drops a drill the show filter has hidden", () => {
    expect(parseDrill("HVAC", ["Car"])).toBeNull();
    expect(parseDrill("HVAC", ["Car", "HVAC"])).toBe("HVAC");
  });

  it("round-trips through parseState (a reload restores the drill)", () => {
    expect(parseState({ range: "7d", show: "HVAC", drill: "HVAC" }).drill).toBe(
      "HVAC",
    );
    expect(parseState({ range: "7d" }).drill).toBeNull();
  });

  it("narrows show to the drilled category — the URL shows what's on screen", () => {
    // Matches the reducer's drill focus, so `drill=X` and `show=X` can't
    // disagree no matter which one the URL happened to carry.
    expect(parseState({ range: "7d", drill: "Lights" }).show).toEqual([
      "Lights",
    ]);
    expect(
      parseState({ range: "7d", show: "Lights,HVAC", drill: "Lights" }).show,
    ).toEqual(["Lights"]);
    expect(parseState({ range: "7d", show: "Lights,HVAC" }).show).toEqual([
      "Lights",
      "HVAC",
    ]);
  });
});
