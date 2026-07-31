import { InfluxDB } from "@influxdata/influxdb-client";
import type { IntervalKey } from "./interval";
import { fluxEvery } from "./interval";
import { categoryFromNameFlux } from "./categories";
import {
  RAW_ENERGY_SOURCE,
  energySourceForSpan,
  needsRawFallback,
  planSegments,
  rollupOffsetMs,
  sourceForInterval,
  type EnergySource,
  type PowerSource,
  type Segment,
} from "./rollup";

const URL = process.env.INFLUX_URL!;
const TOKEN = process.env.INFLUX_TOKEN!;
const ORG = process.env.INFLUX_ORG ?? "home";
const BUCKET = process.env.INFLUX_BUCKET ?? "span";

// CF Access service-token headers — only present in production where the
// origin is gated. In local dev, INFLUX_URL points to the Pi directly via
// LAN or a local tunnel and these can be omitted.
const CF_ID = process.env.CF_ACCESS_CLIENT_ID;
const CF_SECRET = process.env.CF_ACCESS_CLIENT_SECRET;

function makeClient() {
  if (!URL || !TOKEN) {
    throw new Error("INFLUX_URL and INFLUX_TOKEN must be set");
  }
  return new InfluxDB({
    url: URL,
    token: TOKEN,
    // Pi takes a while on wide-range queries (~30d+). Default 10s isn't enough.
    timeout: 120_000,
    headers:
      CF_ID && CF_SECRET
        ? {
            "CF-Access-Client-Id": CF_ID,
            "CF-Access-Client-Secret": CF_SECRET,
          }
        : undefined,
  });
}

export type SeriesPoint = { time: string; series: string; watts: number };

const fluxDate = (ms: number) => new Date(ms).toISOString();

/** ms → a Flux duration literal. Only used for whole-minute rollup buckets. */
const fluxDuration = (ms: number) => `${Math.round(ms / 1000)}s`;

const absValue = `|> map(fn: (r) => ({ r with _value: if r._value < 0.0 then -r._value else r._value }))`;

/**
 * Per-circuit watts → mean per display bucket → category sum. Shared by the raw
 * and rollup pipelines; both feed it one watt-valued row per (circuit, time).
 *
 * createEmpty: false — emitting synthetic rows for empty buckets caused
 * lightweight-charts to throw "Value is null" in its line renderer (it assumes
 * contiguous numeric data and our synthetic-row pipeline must have been leaving
 * a column null). We backfill gaps client-side instead.
 */
function powerRollupTail(interval: IntervalKey): string {
  return `
  |> aggregateWindow(every: ${fluxEvery(interval)}, fn: mean, createEmpty: false)
  |> ${categoryFromNameFlux()}
  |> group(columns: ["_time", "category"])
  |> sum()
  |> group(columns: ["category"])
  |> keep(columns: ["_time", "_value", "category"])`;
}

/** Raw 30s samples. Category is derived from name *after* aggregateWindow, so
 *  the regex runs on ~30×buckets rows instead of every raw point. */
function rawPowerFlux(seg: Segment, interval: IntervalKey): string {
  return `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(seg.startMs)}, stop: ${fluxDate(seg.stopMs)})
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> filter(fn: (r) => exists r._value)
  ${absValue}${powerRollupTail(interval)}
`;
}

/**
 * Pre-aggregated means. The stored timestamp is the *stop* of the period a row
 * covers (Influx's aggregateWindow default), so we shift the query range
 * forward by one bucket to select the rows covering [start, stop), then
 * `timeShift` them back onto start-of-coverage time. timeShift also moves
 * _start/_stop, which is what re-clamps the final partial window to the
 * segment edge.
 */
function rollupPowerFlux(
  seg: Segment,
  interval: IntervalKey,
  src: PowerSource,
): string {
  const off = rollupOffsetMs(src.bucketMs);
  const shift = off > 0 ? `\n  |> timeShift(duration: -${fluxDuration(off)})` : "";
  return `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(seg.startMs + off)}, stop: ${fluxDate(seg.stopMs + off)})
  |> filter(fn: (r) => r._measurement == "${src.measurement}" and r._field == "${src.field}")
  |> filter(fn: (r) => exists r._value)${shift}
  ${absValue}${powerRollupTail(interval)}
`;
}

async function runPowerFlux(flux: string): Promise<SeriesPoint[]> {
  const queryApi = makeClient().getQueryApi(ORG);
  const out: SeriesPoint[] = [];
  for await (const row of queryApi.iterateRows(flux)) {
    const o = row.tableMeta.toObject(row.values) as Record<string, unknown>;
    out.push({
      time: String(o._time),
      series: String(o.category ?? "Other"),
      watts: Number(o._value) || 0,
    });
  }
  return out;
}

/**
 * Mean power per bucket, grouped by derived category. One row per
 * (time, category) — pivot client-side.
 *
 * Reads the coarsest rollup that can fill the requested interval, with a raw
 * tail for the not-yet-rolled-up trailing period (see planSegments).
 */
export async function queryPower(opts: {
  fromMs: number;
  toMs: number;
  interval: IntervalKey;
  /** Injectable for tests; defaults to wall clock. */
  nowMs?: number;
}): Promise<SeriesPoint[]> {
  const { fromMs, toMs, interval } = opts;
  const nowMs = opts.nowMs ?? Date.now();
  const src = sourceForInterval(interval);
  const segments = planSegments({ fromMs, toMs, nowMs, bucketMs: src.bucketMs });

  const parts = await Promise.all(
    segments.map(async (seg) => {
      if (seg.kind === "raw") return runPowerFlux(rawPowerFlux(seg, interval));
      const rows = await runPowerFlux(rollupPowerFlux(seg, interval, src));
      // Rollups don't exist until the Pi-side backfill runs — degrade to raw
      // rather than render an empty chart.
      if (needsRawFallback(rows.length)) {
        return runPowerFlux(rawPowerFlux(seg, interval));
      }
      return rows;
    }),
  );

  // Segments never share a bucket timestamp (the bulk's final window is clamped
  // to the seam; the tail's first window ends strictly after it), so a plain
  // concat is safe. Sort by instant, not by ISO string — RFC3339 fractional
  // seconds don't collate.
  return parts
    .flat()
    .sort((a, b) => Date.parse(a.time) - Date.parse(b.time));
}

export type EnergyRow = { category: string; kwh: number };

/**
 * Integrate each circuit separately to Wh, derive category, then sum by
 * category. Regex after integral() keeps it cheap. integral() requires
 * _start/_stop in the group key.
 * Defensive: skip null/non-finite samples in raw data (one bad point can
 * make integral() emit a huge spurious value), and drop any per-circuit
 * integrals that come out negative (shouldn't happen after abs(), but has
 * been seen on wide ranges — likely sparse-data interpolation artifact).
 */
function rawEnergyFlux(seg: Segment): string {
  return `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(seg.startMs)}, stop: ${fluxDate(seg.stopMs)})
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> filter(fn: (r) => exists r._value)
  ${absValue}
  |> group(columns: ["name", "_start", "_stop"])
  |> integral(unit: 1h)
  |> filter(fn: (r) => r._value >= 0.0)
  |> ${categoryFromNameFlux()}
  |> group(columns: ["category"])
  |> sum()
  |> map(fn: (r) => ({ r with _value: r._value / 1000.0 }))
  |> keep(columns: ["category", "_value"])
`;
}

/**
 * Sum stored per-bucket Wh. No integral, no re-aggregation — exact, and the
 * whole point of the rollups. As in the power path the stored timestamp is the
 * stop of the covered period, so the range is shifted forward one bucket; no
 * timeShift is needed because a sum ignores time.
 */
function rollupEnergyFlux(seg: Segment, src: EnergySource): string {
  const off = rollupOffsetMs(src.bucketMs);
  return `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(seg.startMs + off)}, stop: ${fluxDate(seg.stopMs + off)})
  |> filter(fn: (r) => r._measurement == "${src.measurement}" and r._field == "${src.field}")
  |> filter(fn: (r) => exists r._value)
  |> filter(fn: (r) => r._value >= 0.0)
  |> ${categoryFromNameFlux()}
  |> group(columns: ["category"])
  |> sum()
  |> map(fn: (r) => ({ r with _value: r._value / 1000.0 }))
  |> keep(columns: ["category", "_value"])
`;
}

async function runEnergyFlux(flux: string): Promise<EnergyRow[]> {
  const queryApi = makeClient().getQueryApi(ORG);
  const out: EnergyRow[] = [];
  for await (const row of queryApi.iterateRows(flux)) {
    const o = row.tableMeta.toObject(row.values) as Record<string, unknown>;
    out.push({
      category: String(o.category ?? "Other"),
      kwh: Number(o._value) || 0,
    });
  }
  return out;
}

/**
 * Energy (kWh) per category across the range — one number per category.
 * Drives the breakdown table.
 *
 * Wide windows sum pre-computed `energy_wh` from the rollups; the trailing
 * period the rollup task hasn't covered yet is integrated from raw, so a window
 * ending "now" reports today's energy in full rather than silently short.
 */
export async function queryEnergyByCategory(opts: {
  fromMs: number;
  toMs: number;
  /** Injectable for tests; defaults to wall clock. */
  nowMs?: number;
}): Promise<EnergyRow[]> {
  const { fromMs, toMs } = opts;
  const nowMs = opts.nowMs ?? Date.now();
  const src = energySourceForSpan(toMs - fromMs);
  const segments = planSegments({
    fromMs,
    toMs,
    nowMs,
    bucketMs: src.mode === "sum" ? src.bucketMs : RAW_ENERGY_SOURCE.bucketMs,
  });

  const parts = await Promise.all(
    segments.map(async (seg) => {
      if (seg.kind === "raw") return runEnergyFlux(rawEnergyFlux(seg));
      const rows = await runEnergyFlux(rollupEnergyFlux(seg, src));
      if (needsRawFallback(rows.length)) return runEnergyFlux(rawEnergyFlux(seg));
      return rows;
    }),
  );

  return mergeEnergyRows(parts.flat());
}

/** Segments partition the window, so per-category totals add. */
export function mergeEnergyRows(rows: EnergyRow[]): EnergyRow[] {
  const byCat = new Map<string, number>();
  for (const r of rows) byCat.set(r.category, (byCat.get(r.category) ?? 0) + r.kwh);
  return Array.from(byCat, ([category, kwh]) => ({ category, kwh })).sort(
    (a, b) => b.kwh - a.kwh,
  );
}
