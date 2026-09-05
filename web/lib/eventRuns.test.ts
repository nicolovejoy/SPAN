import { describe, expect, it } from "vitest";
import {
  buildListRows,
  bathsWithin,
  fmtPacificDayRange,
  fmtPacificRange,
  fmtPacificTime,
  formatDurationMs,
  groupModeRuns,
  INTERVAL_MS,
  LIST_CAP,
  zoomWindow,
  type EventItem,
  type EventsPayload,
  type ModeInterval,
} from "./eventRuns";

const T0 = Date.UTC(2026, 8, 4, 19, 0); // 2026-09-04 12:00 PDT
const iv = (i: number, mode: string, kwh = 0.2): ModeInterval => ({
  startMs: T0 + i * INTERVAL_MS,
  mode,
  kwh,
  hpMeanW: 2000 + i * 100,
  hpMaxW: 3000,
  auxMeanW: 0,
});

describe("groupModeRuns", () => {
  it("returns a single-interval run with toMs one interval later", () => {
    const [run] = groupModeRuns([iv(0, "heat")]);
    expect(run).toMatchObject({ mode: "heat", fromMs: T0, toMs: T0 + INTERVAL_MS, intervals: 1 });
  });

  it("joins consecutive same-mode intervals and sums kwh", () => {
    const runs = groupModeRuns([iv(0, "hot_water", 0.3), iv(1, "hot_water", 0.4), iv(2, "hot_water", 0.5)]);
    expect(runs).toHaveLength(1);
    expect(runs[0].kwh).toBeCloseTo(1.2);
    expect(runs[0].toMs).toBe(T0 + 3 * INTERVAL_MS);
    expect(runs[0].hpMeanW).toBeCloseTo(2100); // mean of 2000, 2100, 2200
    expect(runs[0].hpMaxW).toBe(3000);
  });

  it("bridges one missing interval but splits on two", () => {
    const one = groupModeRuns([iv(0, "heat"), iv(2, "heat")]);
    expect(one).toHaveLength(1);
    expect(one[0].toMs).toBe(T0 + 3 * INTERVAL_MS);
    const two = groupModeRuns([iv(0, "heat"), iv(3, "heat")]);
    expect(two).toHaveLength(2);
  });

  it("splits on a mode change and drops idle", () => {
    const runs = groupModeRuns([iv(0, "heat"), iv(1, "idle"), iv(2, "hot_water"), iv(3, "ambiguous")]);
    expect(runs.map((r) => r.mode)).toEqual(["heat", "hot_water", "ambiguous"]);
  });

  it("sorts unsorted input by start", () => {
    const runs = groupModeRuns([iv(1, "cool"), iv(0, "cool")]);
    expect(runs).toHaveLength(1);
    expect(runs[0].fromMs).toBe(T0);
  });

  it("ignores unknown mode strings", () => {
    expect(groupModeRuns([iv(0, "defrost")])).toEqual([]);
  });
});

const bath: EventItem = { kind: "bath", fromMs: T0 + 10 * 60_000, toMs: T0 + 40 * 60_000, kwh: 3.9, costDollars: 0.45, meanW: 2500, maxW: 3400, auxActive: false };
const charge: EventItem = { kind: "charge", fromMs: T0 + 6 * 3600_000, toMs: T0 + 9 * 3600_000, kwh: 31.4, costDollars: 3.64, meanW: 8000, maxW: 9600 };

describe("bathsWithin", () => {
  it("returns baths overlapping the run and ignores charges", () => {
    expect(bathsWithin({ fromMs: T0, toMs: T0 + 3600_000 }, [bath, charge])).toEqual([bath]);
  });
  it("excludes a bath that ends before the run starts", () => {
    expect(bathsWithin({ fromMs: T0 + 3600_000, toMs: T0 + 7200_000 }, [bath])).toEqual([]);
  });
});

describe("buildListRows", () => {
  const cost = (kwh: number) => kwh * 0.1;
  const payload: EventsPayload = {
    modes: groupModeRuns([iv(0, "hot_water", 1), iv(1, "hot_water", 1)]),
    events: [bath, charge],
    modesTruncated: false,
  };

  it("unions modes and events sorted by start with detail text", () => {
    const { rows, total } = buildListRows(payload, cost);
    expect(total).toBe(3);
    expect(rows.map((r) => r.kind)).toEqual(["hot_water", "bath", "charge"]);
    expect(rows[0].detail).toBe("HP 2.1 kW mean · contains 1 bath");
    expect(rows[0].costDollars).toBeCloseTo(0.2);
    expect(rows[1].detail).toBe("HP max 3.4 kW, aux off");
    expect(rows[1].costDollars).toBe(0.45);
    expect(rows[2].detail).toBe("9.6 kW peak");
  });

  it("uses stored cost for events, computed cost for mode runs", () => {
    const { rows } = buildListRows(payload, cost);
    expect(rows.find((r) => r.kind === "charge")!.costDollars).toBe(3.64);
  });

  it("caps at the 50 largest by kWh, re-sorted by start", () => {
    const many: EventsPayload = {
      modes: groupModeRuns(Array.from({ length: 120 }, (_, i) => iv(i * 2, "heat", i % 2 ? 0.1 : 1))),
      events: [],
      modesTruncated: false,
    };
    const { rows, total } = buildListRows(many, cost);
    expect(total).toBe(120);
    expect(rows).toHaveLength(LIST_CAP);
    expect(rows.every((r) => r.kwh === 1)).toBe(true);
    expect(rows.map((r) => r.fromMs)).toEqual([...rows.map((r) => r.fromMs)].sort((a, b) => a - b));
  });

  it("gives every row a stable unique id", () => {
    const { rows } = buildListRows(payload, cost);
    expect(new Set(rows.map((r) => r.id)).size).toBe(rows.length);
  });
});

describe("zoomWindow", () => {
  it("pads 10% each side", () => {
    expect(zoomWindow(0, 3600_000)).toEqual({ fromMs: -360_000, toMs: 3_960_000 });
  });
  it("enforces a 30-minute minimum centred on the event", () => {
    const z = zoomWindow(1_000_000, 1_060_000); // 1 min event
    expect(z.toMs - z.fromMs).toBe(30 * 60_000);
    expect((z.fromMs + z.toMs) / 2).toBe(1_030_000);
  });
});

describe("formatting", () => {
  it("formatDurationMs", () => {
    expect(formatDurationMs(100 * 60_000)).toBe("1h 40m");
    expect(formatDurationMs(30 * 60_000)).toBe("0h 30m");
    expect(formatDurationMs(29.6 * 60_000)).toBe("0h 30m");
  });
  it("fmtPacificTime renders Pacific wall clock", () => {
    expect(fmtPacificTime(T0)).toBe("12:00 PM");
  });
  it("fmtPacificRange adds dates only across a Pacific midnight", () => {
    expect(fmtPacificRange(T0, T0 + 100 * 60_000)).toBe("12:00 PM – 1:40 PM");
    const late = Date.UTC(2026, 8, 5, 6, 30); // 11:30 PM PDT Sep 4
    expect(fmtPacificRange(late, late + 50 * 60_000)).toBe("Sep 4 11:30 PM – Sep 5 12:20 AM");
  });
  it("fmtPacificDayRange", () => {
    expect(fmtPacificDayRange(T0, T0 + 3600_000)).toBe("Sep 4");
    expect(fmtPacificDayRange(T0, T0 + 2 * 86_400_000)).toBe("Sep 4 – Sep 6");
  });
});
