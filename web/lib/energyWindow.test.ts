import { describe, it, expect } from "vitest";
import {
  buildEnergyRows,
  computeDelta,
  hvacModeRowsFromFieldSums,
  mergeDrillRows,
  previousWindowRange,
  spliceChildRows,
  unmonitoredKwh,
} from "./energyWindow";

const DAY = 24 * 60 * 60 * 1000;

describe("previousWindowRange", () => {
  it("returns the immediately-preceding equal-length window", () => {
    const to = 10 * DAY;
    const from = 9 * DAY;
    expect(previousWindowRange(from, to)).toEqual({ fromMs: 8 * DAY, toMs: 9 * DAY });
  });

  it("handles multi-day spans", () => {
    const to = 30 * DAY;
    const from = 23 * DAY; // 7-day window
    expect(previousWindowRange(from, to)).toEqual({ fromMs: 16 * DAY, toMs: 23 * DAY });
  });
});

describe("buildEnergyRows", () => {
  it("attaches prevKwh from the matching category and windowMs to every row", () => {
    const current = [
      { category: "HVAC", kwh: 10 },
      { category: "Kitchen", kwh: 2 },
    ];
    const previous = [{ category: "HVAC", kwh: 8 }];
    const result = buildEnergyRows(current, previous, DAY);
    expect(result).toEqual([
      { category: "HVAC", kwh: 10, prevKwh: 8, windowMs: DAY },
      { category: "Kitchen", kwh: 2, prevKwh: 0, windowMs: DAY },
    ]);
  });

  it("defaults prevKwh to 0 for a category with no prior data", () => {
    const result = buildEnergyRows([{ category: "New", kwh: 5 }], [], DAY);
    expect(result[0].prevKwh).toBe(0);
  });
});

describe("computeDelta", () => {
  it("returns a percent change when the previous window is non-negligible", () => {
    expect(computeDelta(12, 10)).toEqual({ kind: "percent", value: 20 });
    expect(computeDelta(8, 10)).toEqual({ kind: "percent", value: -20 });
  });

  it("falls back to a plain kWh delta when the previous window is ~zero", () => {
    expect(computeDelta(3, 0)).toEqual({ kind: "kwh", value: 3 });
  });

  it("returns none when both windows are ~zero", () => {
    expect(computeDelta(0, 0)).toEqual({ kind: "none" });
  });

  it("returns none when prevKwh is undefined", () => {
    expect(computeDelta(5, undefined)).toEqual({ kind: "none" });
  });
});

describe("unmonitoredKwh", () => {
  it("returns the panel total minus the circuit total", () => {
    expect(unmonitoredKwh(100, 72)).toBe(28);
  });

  it("floors at zero when circuit total exceeds a noisy panel integral", () => {
    expect(unmonitoredKwh(50, 60)).toBe(0);
  });

  it("returns zero when panel and circuits agree exactly", () => {
    expect(unmonitoredKwh(40, 40)).toBe(0);
  });
});

describe("mergeDrillRows", () => {
  const cats = [
    { category: "HVAC", kwh: 10 },
    { category: "Car", kwh: 5 },
  ];
  const circuits = [
    { category: "Heat pump", kwh: 7, parent: "HVAC" },
    { category: "Auxiliary", kwh: 3, parent: "HVAC" },
  ];

  it("splices circuits in right after their parent category", () => {
    expect(
      mergeDrillRows(cats, circuits, "HVAC").map((r) => r.category),
    ).toEqual(["HVAC", "Auxiliary", "Heat pump", "Car"]);
  });

  it("orders circuits by name, matching the chart's shade order", () => {
    const out = mergeDrillRows(cats, circuits, "HVAC").filter((r) => r.parent);
    expect(out.map((r) => r.category)).toEqual(["Auxiliary", "Heat pump"]);
  });

  it("is a no-op with no drill, no rows, or a filtered-out category", () => {
    expect(mergeDrillRows(cats, circuits, null)).toEqual(cats);
    expect(mergeDrillRows(cats, [], "HVAC")).toEqual(cats);
    expect(mergeDrillRows([{ category: "Car", kwh: 5 }], circuits, "HVAC")).toEqual([
      { category: "Car", kwh: 5 },
    ]);
  });

  it("leaves the category row in place as the subtotal", () => {
    const out = mergeDrillRows(cats, circuits, "HVAC");
    const parentRow = out.find((r) => r.category === "HVAC" && !r.parent);
    expect(parentRow?.kwh).toBe(10);
    // Children sum to the parent — the table must not add both into the total.
    expect(out.filter((r) => r.parent).reduce((a, r) => a + r.kwh, 0)).toBe(10);
  });
});

describe("spliceChildRows", () => {
  const rows = [
    { category: "HVAC", kwh: 10 },
    { category: "Lights", kwh: 5 },
    { category: "Unmonitored", kwh: 2 },
    { category: "Heating", kwh: 6, parent: "HVAC" },
    { category: "Hot Water", kwh: 2, parent: "HVAC" },
  ];

  it("moves parent-tagged rows directly after their parent", () => {
    const out = spliceChildRows(rows, "HVAC");
    expect(out.map((r) => r.category)).toEqual([
      "HVAC", "Heating", "Hot Water", "Lights", "Unmonitored",
    ]);
  });

  it("drops children whose parent row is absent", () => {
    const out = spliceChildRows(rows.filter((r) => r.category !== "HVAC"), "HVAC");
    expect(out.every((r) => !r.parent)).toBe(true);
  });

  it("is a no-op when there are no children", () => {
    const plain = rows.filter((r) => !r.parent);
    expect(spliceChildRows(plain, "HVAC")).toEqual(plain);
  });
});

describe("hvacModeRowsFromFieldSums", () => {
  it("maps mode energy fields to nested display rows, dropping ~zero modes", () => {
    const out = hvacModeRowsFromFieldSums({
      energy_heat_kwh: 41.2,
      energy_cool_kwh: 0.001,
      energy_hot_water_kwh: 3.1,
    });
    expect(out).toEqual([
      { category: "Heating", kwh: 41.2, parent: "HVAC" },
      { category: "Hot Water", kwh: 3.1, parent: "HVAC" },
    ]);
  });

  it("returns [] for an empty window (pre-2026 or no data)", () => {
    expect(hvacModeRowsFromFieldSums({})).toEqual([]);
  });
});
