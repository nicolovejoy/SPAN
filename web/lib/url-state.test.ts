import { describe, it, expect } from "vitest";
import { buildIntentSearch } from "./url-state";

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
});
