import { describe, it, expect } from "vitest";
import {
  buildEnergyRows,
  comparisonGrain,
  comparisonLabel,
  computeDelta,
  formatDuration,
  hvacModeRowsFromFieldSums,
  mergeDrillRows,
  paceRanges,
  spliceChildRows,
  unmonitoredKwh,
} from "./energyWindow";

const DAY = 24 * 60 * 60 * 1000;
const HOUR = 60 * 60 * 1000;

describe("comparisonGrain", () => {
  it("picks the calendar grain nearest the viewed window length", () => {
    expect(comparisonGrain(HOUR)).toBe("day");
    expect(comparisonGrain(DAY)).toBe("day");
    expect(comparisonGrain(2 * DAY)).toBe("day");
    expect(comparisonGrain(7 * DAY)).toBe("week");
    expect(comparisonGrain(14 * DAY)).toBe("week");
    expect(comparisonGrain(30 * DAY)).toBe("month");
    expect(comparisonGrain(62 * DAY)).toBe("month");
    expect(comparisonGrain(90 * DAY)).toBe("year");
    expect(comparisonGrain(365 * DAY)).toBe("year");
  });
});

describe("comparisonLabel", () => {
  it("names the prior calendar period for the table header", () => {
    expect(comparisonLabel("day")).toBe("vs yesterday");
    expect(comparisonLabel("week")).toBe("vs last week");
    expect(comparisonLabel("month")).toBe("vs last month");
    expect(comparisonLabel("year")).toBe("vs last year");
  });
});

describe("paceRanges", () => {
  // 2026-08-27 17:00 UTC = 10:00 PDT (a Thursday). Pacific midnight in PDT
  // is 07:00 UTC.
  const anchor = Date.UTC(2026, 7, 27, 17);

  it("day grain: today-so-far vs yesterday through the same time", () => {
    expect(paceRanges(anchor, "day")).toEqual({
      current: { fromMs: Date.UTC(2026, 7, 27, 7), toMs: anchor },
      previous: { fromMs: Date.UTC(2026, 7, 26, 7), toMs: Date.UTC(2026, 7, 26, 17) },
    });
  });

  it("week grain: weeks start Monday Pacific", () => {
    // Aug 27 2026 is a Thursday; its week starts Mon Aug 24.
    expect(paceRanges(anchor, "week")).toEqual({
      current: { fromMs: Date.UTC(2026, 7, 24, 7), toMs: anchor },
      previous: {
        fromMs: Date.UTC(2026, 7, 17, 7),
        toMs: Date.UTC(2026, 7, 20, 17),
      },
    });
  });

  it("week grain: a Sunday belongs to the week of the preceding Monday", () => {
    const sunday = Date.UTC(2026, 7, 23, 17); // Sun Aug 23, 10:00 PDT
    expect(paceRanges(sunday, "week").current.fromMs).toBe(Date.UTC(2026, 7, 17, 7));
  });

  it("month grain: month-to-date vs last month through the same day and time", () => {
    expect(paceRanges(anchor, "month")).toEqual({
      current: { fromMs: Date.UTC(2026, 7, 1, 7), toMs: anchor },
      previous: {
        fromMs: Date.UTC(2026, 6, 1, 7),
        toMs: Date.UTC(2026, 6, 1, 7) + (anchor - Date.UTC(2026, 7, 1, 7)),
      },
    });
  });

  it("month grain: clamps the prior period at its own end when it is shorter", () => {
    // Mar 30: 29+ days elapsed, but February 2026 is 28 days — the prior
    // window must stop at Mar 1 Pacific midnight, not spill into March.
    const lateMarch = Date.UTC(2026, 2, 30, 17); // 10:00 PDT
    const r = paceRanges(lateMarch, "month");
    expect(r.previous.fromMs).toBe(Date.UTC(2026, 1, 1, 8)); // Feb 1, PST midnight
    expect(r.previous.toMs).toBe(Date.UTC(2026, 2, 1, 8)); // Mar 1, PST midnight
  });

  it("year grain: year-to-date vs last year", () => {
    const r = paceRanges(anchor, "year");
    expect(r.current.fromMs).toBe(Date.UTC(2026, 0, 1, 8)); // Jan 1, PST midnight
    expect(r.previous.fromMs).toBe(Date.UTC(2025, 0, 1, 8));
  });

  it("day grain across the spring-forward DST boundary uses each day's own Pacific midnight", () => {
    // DST began Sun Mar 8 2026. Mon Mar 9 midnight is PDT (07:00 UTC);
    // Sun Mar 8 midnight is PST (08:00 UTC).
    const monday = Date.UTC(2026, 2, 9, 19); // Mar 9, 12:00 PDT
    const r = paceRanges(monday, "day");
    expect(r.current.fromMs).toBe(Date.UTC(2026, 2, 9, 7));
    expect(r.previous.fromMs).toBe(Date.UTC(2026, 2, 8, 8));
  });
});

describe("buildEnergyRows", () => {
  it("attaches period/prev-period kWh from matching categories and windowMs to every row", () => {
    const current = [
      { category: "HVAC", kwh: 10 },
      { category: "Kitchen", kwh: 2 },
    ];
    const period = [{ category: "HVAC", kwh: 6 }];
    const prevPeriod = [{ category: "HVAC", kwh: 8 }];
    const result = buildEnergyRows(current, period, prevPeriod, DAY);
    expect(result).toEqual([
      { category: "HVAC", kwh: 10, periodKwh: 6, prevPeriodKwh: 8, windowMs: DAY },
      { category: "Kitchen", kwh: 2, periodKwh: 0, prevPeriodKwh: 0, windowMs: DAY },
    ]);
  });

  it("defaults both period values to 0 for a category with no data there", () => {
    const result = buildEnergyRows([{ category: "New", kwh: 5 }], [], [], DAY);
    expect(result[0].periodKwh).toBe(0);
    expect(result[0].prevPeriodKwh).toBe(0);
  });

  it("stamps periodMs on every row when passed", () => {
    const result = buildEnergyRows(
      [{ category: "HVAC", kwh: 10 }],
      [],
      [],
      DAY,
      3 * HOUR,
    );
    expect(result[0].periodMs).toBe(3 * HOUR);
  });

  it("leaves periodMs undefined when not passed", () => {
    const result = buildEnergyRows([{ category: "HVAC", kwh: 10 }], [], [], DAY);
    expect(result[0].periodMs).toBeUndefined();
  });
});

describe("formatDuration", () => {
  it("under 1h: minutes only", () => {
    expect(formatDuration(45 * 60 * 1000)).toBe("45m");
  });

  it("under 48h: hours with minutes only when nonzero", () => {
    expect(formatDuration(6 * HOUR)).toBe("6h");
    expect(formatDuration(24 * HOUR)).toBe("24h");
    expect(formatDuration(36 * HOUR)).toBe("36h");
    expect(formatDuration(1 * HOUR + 30 * 60 * 1000)).toBe("1h 30m");
  });

  it("48h and up: days with hours only when nonzero", () => {
    expect(formatDuration(10 * DAY)).toBe("10d");
    expect(formatDuration(30 * DAY)).toBe("30d");
    expect(formatDuration(365 * DAY)).toBe("365d");
    expect(formatDuration(3 * DAY + 10 * HOUR)).toBe("3d 10h");
  });

  it("rounds the sub-unit to the nearest whole", () => {
    expect(formatDuration(44.6 * 60 * 1000)).toBe("45m");
    expect(formatDuration(2 * HOUR + 29 * 60 * 1000)).toBe("2h 29m");
  });
});

describe("computeDelta", () => {
  it("returns an absolute kWh delta with a percent when the prior period is big enough", () => {
    expect(computeDelta(12, 10)).toEqual({ kind: "delta", kwh: 2, percent: 20 });
    expect(computeDelta(8, 10)).toEqual({ kind: "delta", kwh: -2, percent: -20 });
  });

  it("omits the percent when the prior period is too small for one to be honest", () => {
    expect(computeDelta(2.3, 0.9)).toEqual({ kind: "delta", kwh: 2.3 - 0.9 });
    expect(computeDelta(3, 0.2)).toEqual({ kind: "delta", kwh: 2.8 });
  });

  it("returns none when both periods are ~zero", () => {
    expect(computeDelta(0, 0)).toEqual({ kind: "none" });
    expect(computeDelta(0.01, 0.02)).toEqual({ kind: "none" });
  });

  it("returns none when the period values are missing", () => {
    expect(computeDelta(undefined, undefined)).toEqual({ kind: "none" });
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
