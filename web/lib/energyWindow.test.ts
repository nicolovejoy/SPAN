import { describe, it, expect } from "vitest";
import {
  buildEnergyRows,
  comparisonGrain,
  computeDelta,
  hvacModeRowsFromFieldSums,
  mergeDrillRows,
  periodLabel,
  prevPeriodLabel,
  previousPeriodStart,
  snapPeriod,
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

describe("snapPeriod", () => {
  // 2026-08-27 17:00 UTC = 10:00 PDT (a Thursday). Pacific midnight in PDT
  // is 07:00 UTC. Aug 24 2026 is the Monday starting that week.
  const nowRef = Date.UTC(2026, 7, 27, 17);

  it("partial day: today-so-far vs yesterday through the same time", () => {
    expect(snapPeriod(nowRef, "day", nowRef)).toEqual({
      fromMs: Date.UTC(2026, 7, 27, 7),
      toMs: nowRef,
      complete: false,
      previous: { fromMs: Date.UTC(2026, 7, 26, 7), toMs: Date.UTC(2026, 7, 26, 17) },
    });
  });

  it("partial week: weeks start Monday Pacific", () => {
    expect(snapPeriod(nowRef, "week", nowRef)).toEqual({
      fromMs: Date.UTC(2026, 7, 24, 7),
      toMs: nowRef,
      complete: false,
      previous: { fromMs: Date.UTC(2026, 7, 17, 7), toMs: Date.UTC(2026, 7, 20, 17) },
    });
  });

  it("week grain: a Sunday belongs to the week of the preceding Monday", () => {
    const sunday = Date.UTC(2026, 7, 23, 17); // Sun Aug 23, 10:00 PDT
    expect(snapPeriod(sunday, "week", sunday).fromMs).toBe(Date.UTC(2026, 7, 17, 7));
  });

  it("partial month: month-to-date vs last month through the same day and time", () => {
    const r = snapPeriod(nowRef, "month", nowRef);
    expect(r.complete).toBe(false);
    expect(r.fromMs).toBe(Date.UTC(2026, 7, 1, 7));
    expect(r.toMs).toBe(nowRef);
    expect(r.previous).toEqual({
      fromMs: Date.UTC(2026, 6, 1, 7),
      toMs: Date.UTC(2026, 6, 1, 7) + (nowRef - Date.UTC(2026, 7, 1, 7)),
    });
  });

  it("partial month: clamps the prior period at its own end when it is shorter", () => {
    // Mar 30: 29+ days elapsed, but February 2026 is 28 days — the prior
    // window must stop at Mar 1 Pacific midnight, not spill into March.
    const lateMarch = Date.UTC(2026, 2, 30, 17); // 10:00 PDT
    const r = snapPeriod(lateMarch, "month", lateMarch);
    expect(r.complete).toBe(false);
    expect(r.previous.fromMs).toBe(Date.UTC(2026, 1, 1, 8)); // Feb 1, PST midnight
    expect(r.previous.toMs).toBe(Date.UTC(2026, 2, 1, 8)); // Mar 1, PST midnight
  });

  it("partial year: year-to-date vs last year", () => {
    const r = snapPeriod(nowRef, "year", nowRef);
    expect(r.complete).toBe(false);
    expect(r.fromMs).toBe(Date.UTC(2026, 0, 1, 8)); // Jan 1, PST midnight
    expect(r.previous.fromMs).toBe(Date.UTC(2025, 0, 1, 8));
  });

  it("day grain across the spring-forward DST boundary uses each day's own Pacific midnight", () => {
    // DST began Sun Mar 8 2026. Mon Mar 9 midnight is PDT (07:00 UTC);
    // Sun Mar 8 midnight is PST (08:00 UTC).
    const monday = Date.UTC(2026, 2, 9, 19); // Mar 9, 12:00 PDT
    const r = snapPeriod(monday, "day", monday);
    expect(r.fromMs).toBe(Date.UTC(2026, 2, 9, 7));
    expect(r.previous.fromMs).toBe(Date.UTC(2026, 2, 8, 8));
  });

  it("complete day: full prior day, no clamp", () => {
    const anchor = Date.UTC(2026, 7, 20, 17); // Aug 20, 10:00 PDT — well before nowRef
    const r = snapPeriod(anchor, "day", nowRef);
    expect(r).toEqual({
      fromMs: Date.UTC(2026, 7, 20, 7),
      toMs: Date.UTC(2026, 7, 21, 7),
      complete: true,
      previous: { fromMs: Date.UTC(2026, 7, 19, 7), toMs: Date.UTC(2026, 7, 20, 7) },
    });
  });

  it("complete week: full prior week, no clamp", () => {
    const anchor = Date.UTC(2026, 7, 20, 17); // within the week of Aug 17-24
    const r = snapPeriod(anchor, "week", nowRef);
    expect(r).toEqual({
      fromMs: Date.UTC(2026, 7, 17, 7),
      toMs: Date.UTC(2026, 7, 24, 7),
      complete: true,
      previous: { fromMs: Date.UTC(2026, 7, 10, 7), toMs: Date.UTC(2026, 7, 17, 7) },
    });
  });

  it("complete month: full prior month, no clamp", () => {
    const anchor = Date.UTC(2026, 6, 15, 17); // mid-July, well before nowRef (late Aug)
    const r = snapPeriod(anchor, "month", nowRef);
    expect(r).toEqual({
      fromMs: Date.UTC(2026, 6, 1, 7),
      toMs: Date.UTC(2026, 7, 1, 7),
      complete: true,
      previous: { fromMs: Date.UTC(2026, 5, 1, 7), toMs: Date.UTC(2026, 6, 1, 7) },
    });
  });

  it("complete year: full prior year, no clamp", () => {
    const anchor = Date.UTC(2025, 5, 15, 17); // mid-2025
    const r = snapPeriod(anchor, "year", nowRef);
    expect(r).toEqual({
      fromMs: Date.UTC(2025, 0, 1, 8),
      toMs: Date.UTC(2026, 0, 1, 8),
      complete: true,
      previous: { fromMs: Date.UTC(2024, 0, 1, 8), toMs: Date.UTC(2025, 0, 1, 8) },
    });
  });

  it("anchor at an exact midnight boundary picks the just-finished period, not an empty new one", () => {
    const boundary = Date.UTC(2026, 7, 27, 7); // exactly Pacific midnight, Aug 27
    const later = Date.UTC(2026, 7, 27, 17); // 10:00 PDT the same day
    const r = snapPeriod(boundary, "day", later);
    // Lands in Aug 26 (the day that just finished), fully complete.
    expect(r).toEqual({
      fromMs: Date.UTC(2026, 7, 26, 7),
      toMs: Date.UTC(2026, 7, 27, 7),
      complete: true,
      previous: { fromMs: Date.UTC(2026, 7, 25, 7), toMs: Date.UTC(2026, 7, 26, 7) },
    });
  });

  it("anchor in the future clamps to now", () => {
    const future = Date.UTC(2026, 8, 1, 17); // Sept 1 — after nowRef
    const r = snapPeriod(future, "day", nowRef);
    // Snaps to the period containing "now" (today-so-far), not the future day.
    expect(r.fromMs).toBe(Date.UTC(2026, 7, 27, 7));
    expect(r.toMs).toBe(nowRef);
    expect(r.complete).toBe(false);
  });
});

describe("previousPeriodStart", () => {
  it("agrees with snapPeriod's own previous.fromMs (pure calendar step, no now)", () => {
    const nowRef = Date.UTC(2026, 7, 27, 17);
    for (const grain of ["day", "week", "month", "year"] as const) {
      const snap = snapPeriod(nowRef, grain, nowRef);
      expect(previousPeriodStart(snap.fromMs, grain)).toBe(snap.previous.fromMs);
    }
  });
});

describe("periodLabel", () => {
  it("labels each grain's period start", () => {
    expect(periodLabel(Date.UTC(2026, 5, 16, 7), "day")).toBe("Tue Jun 16");
    expect(periodLabel(Date.UTC(2026, 7, 24, 7), "week")).toBe("Week of Aug 24");
    expect(periodLabel(Date.UTC(2026, 7, 1, 7), "month")).toBe("Aug 2026");
    expect(periodLabel(Date.UTC(2026, 0, 1, 8), "year")).toBe("2026");
  });
});

describe("prevPeriodLabel", () => {
  it("labels each grain's prior period start", () => {
    expect(prevPeriodLabel(Date.UTC(2026, 5, 15, 7), "day")).toBe("vs Mon Jun 15");
    expect(prevPeriodLabel(Date.UTC(2026, 7, 17, 7), "week")).toBe("vs week of Aug 17");
    expect(prevPeriodLabel(Date.UTC(2026, 6, 1, 7), "month")).toBe("vs Jul");
    expect(prevPeriodLabel(Date.UTC(2025, 0, 1, 8), "year")).toBe("vs 2025");
  });
});

describe("buildEnergyRows", () => {
  const meta = {
    periodFromMs: 100,
    periodToMs: 200,
    periodGrain: "day" as const,
    periodComplete: true,
  };

  it("attaches prevPeriodKwh from matching categories and stamps meta on every row", () => {
    const current = [
      { category: "HVAC", kwh: 10 },
      { category: "Kitchen", kwh: 2 },
    ];
    const prevPeriod = [{ category: "HVAC", kwh: 8 }];
    const result = buildEnergyRows(current, prevPeriod, meta);
    expect(result).toEqual([
      { category: "HVAC", kwh: 10, prevPeriodKwh: 8, ...meta },
      { category: "Kitchen", kwh: 2, prevPeriodKwh: 0, ...meta },
    ]);
  });

  it("defaults prevPeriodKwh to 0 for a category with no data in the prior period", () => {
    const result = buildEnergyRows([{ category: "New", kwh: 5 }], [], meta);
    expect(result[0].prevPeriodKwh).toBe(0);
  });

  it("stamps periodComplete: false for a partial period", () => {
    const result = buildEnergyRows([{ category: "HVAC", kwh: 10 }], [], {
      ...meta,
      periodComplete: false,
    });
    expect(result[0].periodComplete).toBe(false);
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
