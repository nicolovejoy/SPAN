// Source selection for pre-aggregated Influx rollups (issue #9).
//
// The Pi writes two downsampled measurements into the same `span` bucket,
// derived from raw `circuit`/`power_w`:
//
//   circuit_5m, circuit_1h
//     tags:   name, circuit_id
//     fields: power_w_mean      — mean of abs(power_w) over the bucket
//             energy_wh         — integral(unit: 1h) of abs(power_w), in Wh
//             energy_wh_counter — increase in SPAN's own consumed_energy_wh
//                                 meter across the bucket. Not read here yet;
//                                 kept alongside energy_wh so the two can be
//                                 A/B'd over real history before either is
//                                 made authoritative.
//
// Raw `circuit` is retained forever, so raw is always a valid fallback and the
// rollups are a pure speed optimisation. Everything here is pure: the Flux
// generation and the I/O live in ./influx.ts.

import type { IntervalKey } from "./interval";

export type MeasurementId = "circuit" | "circuit_5m" | "circuit_1h";

export const MINUTE_MS = 60_000;
export const HOUR_MS = 60 * MINUTE_MS;
export const DAY_MS = 24 * HOUR_MS;

/**
 * Which stored timestamp a rollup point carries. Influx's `aggregateWindow`
 * defaults to `timeSrc: "_stop"`, so a point at time T covers [T - bucket, T).
 *
 * If the Pi-side task is ever changed to `timeSrc: "_start"`, flip this one
 * constant — everything downstream (range shifting, timeShift normalisation)
 * is derived from it.
 */
export const ROLLUP_STAMP_AT: "start" | "stop" = "stop";

/**
 * How far a stored rollup timestamp sits ahead of the start of the period it
 * covers. Add this to a wall-clock window to select the rollup rows covering
 * that window; subtract it (Flux `timeShift`) to put the rows back on
 * start-of-coverage time.
 */
export function rollupOffsetMs(bucketMs: number): number {
  return ROLLUP_STAMP_AT === "stop" ? bucketMs : 0;
}

export type PowerSource = {
  measurement: MeasurementId;
  field: "power_w" | "power_w_mean";
  /** Width of one stored bucket in ms; 0 for raw (instantaneous 30s samples). */
  bucketMs: number;
};

export const RAW_POWER_SOURCE: PowerSource = {
  measurement: "circuit",
  field: "power_w",
  bucketMs: 0,
};

const ROLLUP_5M_POWER: PowerSource = {
  measurement: "circuit_5m",
  field: "power_w_mean",
  bucketMs: 5 * MINUTE_MS,
};

const ROLLUP_1H_POWER: PowerSource = {
  measurement: "circuit_1h",
  field: "power_w_mean",
  bucketMs: HOUR_MS,
};

/**
 * Coarsest source that can still fill the requested display bucket.
 *
 *   1m              → raw `circuit`
 *   5m, 15m         → `circuit_5m`
 *   1h, 6h, 1d, 1w  → `circuit_1h`
 *
 * The source bucket is always ≤ the display interval, so re-aggregating with
 * `aggregateWindow(fn: mean)` never has to split a stored bucket.
 *
 * NOTE (approximation): re-aggregating stored means with `mean` is a
 * mean-of-means. That equals the true mean only when every source bucket holds
 * the same number of raw samples. The collector polls on a fixed 30s cadence so
 * that's true except across dropouts, where a sparse bucket gets the same weight
 * as a full one. Good enough for a chart line; the breakdown *table* does not
 * rely on this — it sums `energy_wh_counter`, which is exact regardless.
 */
export function sourceForInterval(interval: IntervalKey): PowerSource {
  switch (interval) {
    case "1m":
      return RAW_POWER_SOURCE;
    case "5m":
    case "15m":
      return ROLLUP_5M_POWER;
    default:
      return ROLLUP_1H_POWER;
  }
}

export type EnergySource = {
  measurement: MeasurementId;
  /** "integral": derive Wh from raw watt samples. "sum": add stored Wh. */
  mode: "integral" | "sum";
  field: "power_w" | "energy_wh" | "energy_wh_counter";
  bucketMs: number;
};

/** Windows up to this span integrate raw points (most accurate). */
export const ENERGY_RAW_MAX_MS = 48 * HOUR_MS;
/** Windows up to this span sum `circuit_5m.energy_wh_counter`. */
export const ENERGY_5M_MAX_MS = 7 * DAY_MS;

export const RAW_ENERGY_SOURCE: EnergySource = {
  measurement: "circuit",
  mode: "integral",
  field: "power_w",
  bucketMs: 0,
};

/**
 * Energy source by window span:
 *
 *   ≤ 48h → raw integral (existing pipeline, most accurate at fine buckets)
 *   ≤ 7d  → sum(circuit_5m.energy_wh_counter)
 *   > 7d  → sum(circuit_1h.energy_wh_counter)
 *
 * Summing pre-computed Wh is *exact* — there is no re-integration error — which
 * is why this is the big win over `queryPower`'s mean-of-means.
 *
 * Since #15 the summed field is `energy_wh_counter`, the delta of SPAN's own
 * cumulative meter, rather than `energy_wh`, the integral of our 30s samples.
 * The counter keeps ticking inside the panel whether or not we poll, so a
 * missed poll costs nothing; the integral interpolates across the gap and
 * invents energy that was never measured. `energy_wh` is still stored as a
 * cross-check. Short windows stay on the raw integral because the counter is
 * too coarse for fine buckets.
 *
 * The 5m→1h boundary was originally 30d, but `circuit_5m` returns ~12x more
 * points per unit time than `circuit_1h` (18,144/day vs 1,512/day), so a 30d
 * `circuit_5m` query (~181k pts, 7.9s) ended up slower than a 1y `circuit_1h`
 * query (~105k pts, 4.7s) — the coarser rollup should take over well before
 * that crossover. 7d keeps `circuit_5m` in play only for windows short enough
 * that its extra point count doesn't matter.
 */
export function energySourceForSpan(spanMs: number): EnergySource {
  if (spanMs <= ENERGY_RAW_MAX_MS) return RAW_ENERGY_SOURCE;
  if (spanMs <= ENERGY_5M_MAX_MS) {
    return {
      measurement: "circuit_5m",
      mode: "sum",
      field: "energy_wh_counter",
      bucketMs: 5 * MINUTE_MS,
    };
  }
  return {
    measurement: "circuit_1h",
    mode: "sum",
    field: "energy_wh_counter",
    bucketMs: HOUR_MS,
  };
}

/**
 * Newest wall-clock instant the rollup can be trusted to cover.
 *
 * The rollup task runs on a schedule, so at any moment the newest bucket is
 * (a) still open and (b) possibly not yet written even if closed. We therefore
 * trust the rollup only up to the end of the *second-newest* bucket boundary,
 * i.e. one full bucket of slack for task lag. Anything after that is read from
 * raw (see planSegments) — at most 2 bucket-widths of raw data, which is 2h of
 * 30s points at worst (~3.6k points). Cheap, and it means the trailing window
 * is never under-reported.
 *
 * Returns +Infinity for raw sources (no lag, no bucketing).
 */
export function rollupCutoffMs(nowMs: number, bucketMs: number): number {
  if (bucketMs <= 0) return Infinity;
  return Math.floor(nowMs / bucketMs) * bucketMs - bucketMs;
}

export type Segment = {
  /** "rollup" reads the downsampled measurement; "raw" reads `circuit`. */
  kind: "rollup" | "raw";
  startMs: number;
  stopMs: number;
};

/**
 * Split [fromMs, toMs) into a rollup bulk plus a raw tail, so a window ending
 * near `now` still sees the freshest data.
 *
 * The seam is bucket-aligned (it comes out of `rollupCutoffMs`) except when
 * clamped to the window edges, which keeps the energy sum from double-counting
 * or dropping a bucket at the join.
 *
 * Left-edge caveat for energy: a rollup bucket straddling `fromMs` is excluded
 * whole (its stored timestamp falls outside the shifted range), so a rollup sum
 * can under-count by up to one bucket at the left edge — ≤1h on a window that
 * is by definition >30d (<0.15%). Raw integrals have the same edge fuzz.
 */
export function planSegments(opts: {
  fromMs: number;
  toMs: number;
  nowMs: number;
  bucketMs: number;
}): Segment[] {
  const { fromMs, toMs, nowMs, bucketMs } = opts;
  if (toMs <= fromMs) return [];
  if (bucketMs <= 0) return [{ kind: "raw", startMs: fromMs, stopMs: toMs }];

  const cutoff = rollupCutoffMs(nowMs, bucketMs);
  const seam = Math.min(Math.max(cutoff, fromMs), toMs);

  const segs: Segment[] = [];
  if (seam > fromMs) segs.push({ kind: "rollup", startMs: fromMs, stopMs: seam });
  if (toMs > seam) segs.push({ kind: "raw", startMs: seam, stopMs: toMs });
  return segs;
}

/**
 * A rollup segment that came back with zero rows means the measurement isn't
 * populated for that window — most likely the Pi-side task/backfill hasn't run
 * yet. Re-run the segment against raw rather than render an empty chart/table.
 * (A genuinely empty window costs one wasted query and still returns nothing,
 * which is the safe direction.)
 */
export function needsRawFallback(rowCount: number): boolean {
  return rowCount === 0;
}

/** Short, stable id for cache keys so two sources can never share an entry. */
export function sourceKey(src: { measurement: MeasurementId }): string {
  return src.measurement;
}
