import { describe, it, expect } from "vitest";
import {
  DAY_MS,
  ENERGY_5M_MAX_MS,
  ENERGY_RAW_MAX_MS,
  HOUR_MS,
  MINUTE_MS,
  ROLLUP_STAMP_AT,
  energySourceForSpan,
  needsRawFallback,
  planSegments,
  rollupCutoffMs,
  rollupOffsetMs,
  sourceForInterval,
  sourceKey,
} from "./rollup";
import { mergeEnergyRows } from "./influx";

describe("sourceForInterval", () => {
  it("keeps 1m on raw circuit", () => {
    expect(sourceForInterval("1m")).toMatchObject({
      measurement: "circuit",
      field: "power_w",
      bucketMs: 0,
    });
  });

  it("uses the 5m rollup for 5m and 15m", () => {
    for (const k of ["5m", "15m"] as const) {
      expect(sourceForInterval(k)).toMatchObject({
        measurement: "circuit_5m",
        field: "power_w_mean",
        bucketMs: 5 * MINUTE_MS,
      });
    }
  });

  it("uses the 1h rollup for 1h and coarser", () => {
    for (const k of ["1h", "6h", "1d", "1w"] as const) {
      expect(sourceForInterval(k)).toMatchObject({
        measurement: "circuit_1h",
        field: "power_w_mean",
        bucketMs: HOUR_MS,
      });
    }
  });

  it("never picks a source bucket coarser than the display interval", () => {
    const intervalMs = {
      "1m": MINUTE_MS,
      "5m": 5 * MINUTE_MS,
      "15m": 15 * MINUTE_MS,
      "1h": HOUR_MS,
      "6h": 6 * HOUR_MS,
      "1d": DAY_MS,
      "1w": 7 * DAY_MS,
    } as const;
    for (const [k, ms] of Object.entries(intervalMs)) {
      const src = sourceForInterval(k as keyof typeof intervalMs);
      expect(src.bucketMs).toBeLessThanOrEqual(ms);
    }
  });
});

describe("energySourceForSpan", () => {
  it("integrates raw up to 48h inclusive", () => {
    expect(energySourceForSpan(HOUR_MS)).toMatchObject({
      measurement: "circuit",
      mode: "integral",
    });
    expect(energySourceForSpan(ENERGY_RAW_MAX_MS)).toMatchObject({
      measurement: "circuit",
      mode: "integral",
    });
  });

  it("sums the 5m rollup just past 48h and up to 30d inclusive", () => {
    expect(energySourceForSpan(ENERGY_RAW_MAX_MS + 1)).toMatchObject({
      measurement: "circuit_5m",
      mode: "sum",
      field: "energy_wh",
    });
    expect(energySourceForSpan(ENERGY_5M_MAX_MS)).toMatchObject({
      measurement: "circuit_5m",
      mode: "sum",
    });
  });

  it("sums the 1h rollup past 30d", () => {
    expect(energySourceForSpan(ENERGY_5M_MAX_MS + 1)).toMatchObject({
      measurement: "circuit_1h",
      mode: "sum",
      bucketMs: HOUR_MS,
    });
    expect(energySourceForSpan(365 * DAY_MS)).toMatchObject({
      measurement: "circuit_1h",
      mode: "sum",
    });
  });
});

describe("rollupOffsetMs", () => {
  it("matches the stamping convention", () => {
    // Guards the one constant the Pi-side task convention has to agree with.
    expect(ROLLUP_STAMP_AT).toBe("stop");
    expect(rollupOffsetMs(HOUR_MS)).toBe(HOUR_MS);
    expect(rollupOffsetMs(0)).toBe(0);
  });
});

describe("rollupCutoffMs", () => {
  const noonMs = Date.UTC(2026, 5, 19, 12, 0, 0);

  it("is infinite for raw (no task lag)", () => {
    expect(rollupCutoffMs(noonMs, 0)).toBe(Infinity);
  });

  it("trusts the rollup only through the second-newest closed bucket", () => {
    // exactly on a boundary: newest closed bucket ends at 12:00, allow 1h slack
    expect(rollupCutoffMs(noonMs, HOUR_MS)).toBe(noonMs - HOUR_MS);
    // mid-bucket: 12:37 → open bucket started 12:00, trust through 11:00
    expect(rollupCutoffMs(noonMs + 37 * MINUTE_MS, HOUR_MS)).toBe(noonMs - HOUR_MS);
  });

  it("always lands on a bucket boundary", () => {
    for (const bucket of [5 * MINUTE_MS, HOUR_MS]) {
      const c = rollupCutoffMs(noonMs + 12_345, bucket);
      expect(c % bucket).toBe(0);
    }
  });

  it("leaves a raw tail of at most two bucket widths", () => {
    for (const bucket of [5 * MINUTE_MS, HOUR_MS]) {
      for (const skew of [0, 1, 59_000, bucket - 1]) {
        const now = noonMs + skew;
        const tail = now - rollupCutoffMs(now, bucket);
        expect(tail).toBeGreaterThanOrEqual(bucket);
        expect(tail).toBeLessThanOrEqual(2 * bucket);
      }
    }
  });
});

describe("planSegments", () => {
  const now = Date.UTC(2026, 5, 19, 12, 30, 0);

  it("is a single raw segment for a raw source", () => {
    expect(
      planSegments({ fromMs: now - DAY_MS, toMs: now, nowMs: now, bucketMs: 0 }),
    ).toEqual([{ kind: "raw", startMs: now - DAY_MS, stopMs: now }]);
  });

  it("returns nothing for an empty or inverted window", () => {
    expect(planSegments({ fromMs: now, toMs: now, nowMs: now, bucketMs: HOUR_MS })).toEqual([]);
    expect(
      planSegments({ fromMs: now, toMs: now - 1, nowMs: now, bucketMs: HOUR_MS }),
    ).toEqual([]);
  });

  it("splits a trailing window into rollup bulk + raw tail", () => {
    const from = now - 90 * DAY_MS;
    const segs = planSegments({ fromMs: from, toMs: now, nowMs: now, bucketMs: HOUR_MS });
    const seam = Date.UTC(2026, 5, 19, 11, 0, 0);
    expect(segs).toEqual([
      { kind: "rollup", startMs: from, stopMs: seam },
      { kind: "raw", startMs: seam, stopMs: now },
    ]);
  });

  it("covers the window exactly — no gap, no overlap", () => {
    const from = now - 30 * DAY_MS;
    const segs = planSegments({ fromMs: from, toMs: now, nowMs: now, bucketMs: 5 * MINUTE_MS });
    expect(segs[0].startMs).toBe(from);
    expect(segs[segs.length - 1].stopMs).toBe(now);
    for (let i = 1; i < segs.length; i++) {
      expect(segs[i].startMs).toBe(segs[i - 1].stopMs);
    }
  });

  it("is pure rollup for a wholly historical window", () => {
    const to = now - 10 * DAY_MS;
    const from = to - 90 * DAY_MS;
    expect(planSegments({ fromMs: from, toMs: to, nowMs: now, bucketMs: HOUR_MS })).toEqual([
      { kind: "rollup", startMs: from, stopMs: to },
    ]);
  });

  it("is pure raw when the whole window is inside the un-rolled-up tail", () => {
    const from = now - 20 * MINUTE_MS;
    const segs = planSegments({ fromMs: from, toMs: now, nowMs: now, bucketMs: HOUR_MS });
    expect(segs).toEqual([{ kind: "raw", startMs: from, stopMs: now }]);
  });

  it("keeps the seam on a bucket boundary so summed energy can't double-count", () => {
    const from = now - 365 * DAY_MS;
    const [bulk] = planSegments({ fromMs: from, toMs: now, nowMs: now, bucketMs: HOUR_MS });
    expect(bulk.stopMs % HOUR_MS).toBe(0);
  });
});

describe("needsRawFallback", () => {
  it("falls back when the rollup measurement yields nothing", () => {
    expect(needsRawFallback(0)).toBe(true);
  });
  it("keeps rollup rows when there are any", () => {
    expect(needsRawFallback(1)).toBe(false);
    expect(needsRawFallback(4200)).toBe(false);
  });
});

describe("sourceKey", () => {
  it("distinguishes the three measurements", () => {
    const keys = new Set([
      sourceKey(sourceForInterval("1m")),
      sourceKey(sourceForInterval("5m")),
      sourceKey(sourceForInterval("1h")),
    ]);
    expect(keys).toEqual(new Set(["circuit", "circuit_5m", "circuit_1h"]));
  });
});

describe("mergeEnergyRows", () => {
  it("adds per-category totals across segments and sorts descending", () => {
    expect(
      mergeEnergyRows([
        { category: "HVAC", kwh: 10 },
        { category: "Car", kwh: 1 },
        { category: "HVAC", kwh: 2.5 },
      ]),
    ).toEqual([
      { category: "HVAC", kwh: 12.5 },
      { category: "Car", kwh: 1 },
    ]);
  });

  it("is empty for no rows", () => {
    expect(mergeEnergyRows([])).toEqual([]);
  });
});
