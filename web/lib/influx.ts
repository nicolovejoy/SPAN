import { InfluxDB } from "@influxdata/influxdb-client";
import type { IntervalKey } from "./interval";
import { fluxEvery } from "./interval";
import { categoryFromNameFlux, nameMatchesCategoriesFlux } from "./categories";
import { unmonitoredKwh } from "./energyWindow";
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

/**
 * How rows are collapsed before they leave Influx.
 *
 *   category — one series per derived category (the default 5-line view)
 *   circuit  — one series per individual circuit *within* one category, i.e.
 *              the drill-down (#12). Membership comes from the same regex rules
 *              as `category` (see ./categories), pushed into the query so we
 *              never pull series we'd only discard.
 *
 * Both shapes come back as `series` on SeriesPoint / `category` on EnergyRow —
 * the caller knows which it asked for.
 */
export type Grouping =
  | { kind: "category" }
  | { kind: "circuit"; category: string };

const CATEGORY_GROUPING: Grouping = { kind: "category" };

/** Extra predicate narrowing the scan to one category's circuits. */
function groupingFilter(g: Grouping): string {
  return g.kind === "circuit"
    ? ` and ${nameMatchesCategoriesFlux([g.category])}`
    : "";
}

const fluxDate = (ms: number) => new Date(ms).toISOString();

/** ms → a Flux duration literal. Only used for whole-minute rollup buckets. */
const fluxDuration = (ms: number) => `${Math.round(ms / 1000)}s`;

const absValue = `|> map(fn: (r) => ({ r with _value: if r._value < 0.0 then -r._value else r._value }))`;

/**
 * Per-circuit watts → mean per display bucket → sum per output series. Shared by
 * the raw and rollup pipelines; both feed it one watt-valued row per
 * (circuit, time).
 *
 * Category grouping derives `category` from the name and sums the circuits into
 * it. Circuit grouping keeps `name` as the series key and only sums across
 * duplicate `circuit_id`s sharing one name — the same collapse the category
 * path does, one level down.
 *
 * createEmpty: false — emitting synthetic rows for empty buckets caused
 * lightweight-charts to throw "Value is null" in its line renderer (it assumes
 * contiguous numeric data and our synthetic-row pipeline must have been leaving
 * a column null). We backfill gaps client-side instead.
 */
function powerRollupTail(interval: IntervalKey, g: Grouping): string {
  const head = `
  |> aggregateWindow(every: ${fluxEvery(interval)}, fn: mean, createEmpty: false)`;
  if (g.kind === "circuit") {
    return `${head}
  |> group(columns: ["_time", "name"])
  |> sum()
  |> group(columns: ["name"])
  |> keep(columns: ["_time", "_value", "name"])`;
  }
  return `${head}
  |> ${categoryFromNameFlux()}
  |> group(columns: ["_time", "category"])
  |> sum()
  |> group(columns: ["category"])
  |> keep(columns: ["_time", "_value", "category"])`;
}

/** Raw 30s samples. Category is derived from name *after* aggregateWindow, so
 *  the regex runs on ~30×buckets rows instead of every raw point. (A drilled
 *  query still filters by name up front — there it narrows the scan.) */
function rawPowerFlux(seg: Segment, interval: IntervalKey, g: Grouping): string {
  return `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(seg.startMs)}, stop: ${fluxDate(seg.stopMs)})
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> filter(fn: (r) => exists r._value${groupingFilter(g)})
  ${absValue}${powerRollupTail(interval, g)}
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
  g: Grouping,
): string {
  const off = rollupOffsetMs(src.bucketMs);
  const shift = off > 0 ? `\n  |> timeShift(duration: -${fluxDuration(off)})` : "";
  return `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(seg.startMs + off)}, stop: ${fluxDate(seg.stopMs + off)})
  |> filter(fn: (r) => r._measurement == "${src.measurement}" and r._field == "${src.field}")
  |> filter(fn: (r) => exists r._value${groupingFilter(g)})${shift}
  ${absValue}${powerRollupTail(interval, g)}
`;
}

async function runPowerFlux(flux: string): Promise<SeriesPoint[]> {
  const queryApi = makeClient().getQueryApi(ORG);
  const out: SeriesPoint[] = [];
  for await (const row of queryApi.iterateRows(flux)) {
    const o = row.tableMeta.toObject(row.values) as Record<string, unknown>;
    out.push({
      time: String(o._time),
      // `category` on the grouped-by-category pipeline, `name` on the drilled
      // one — only one of the two columns survives the final keep().
      series: String(o.category ?? o.name ?? "Other"),
      watts: Number(o._value) || 0,
    });
  }
  return out;
}

/**
 * Time of the newest point for a measurement/field, or null if none exists in
 * the lookback window. Used by /api/health to alarm on artifact age.
 */
export async function queryLastPointTime(
  measurement: string,
  field: string,
  lookback: string,
): Promise<string | null> {
  const flux = `
    from(bucket: "${BUCKET}")
      |> range(start: -${lookback})
      |> filter(fn: (r) => r._measurement == "${measurement}" and r._field == "${field}")
      |> group()
      |> last()
      |> keep(columns: ["_time"])`;
  const rows = await makeClient()
    .getQueryApi(ORG)
    .collectRows<{ _time: string }>(flux);
  return rows[0]?._time ?? null;
}

/**
 * Mean power per bucket, grouped by derived category — or, when `grouping` is a
 * drill, by circuit within one category. One row per (time, series) — pivot
 * client-side.
 *
 * Reads the coarsest rollup that can fill the requested interval, with a raw
 * tail for the not-yet-rolled-up trailing period (see planSegments). The `name`
 * tag survives into `circuit_5m`/`circuit_1h`, so source selection is identical
 * either way.
 */
export async function queryPower(opts: {
  fromMs: number;
  toMs: number;
  interval: IntervalKey;
  grouping?: Grouping;
  /** Injectable for tests; defaults to wall clock. */
  nowMs?: number;
}): Promise<SeriesPoint[]> {
  const { fromMs, toMs, interval } = opts;
  const g = opts.grouping ?? CATEGORY_GROUPING;
  const nowMs = opts.nowMs ?? Date.now();
  const src = sourceForInterval(interval);
  const segments = planSegments({ fromMs, toMs, nowMs, bucketMs: src.bucketMs });

  const parts = await Promise.all(
    segments.map(async (seg) => {
      if (seg.kind === "raw") return runPowerFlux(rawPowerFlux(seg, interval, g));
      const rows = await runPowerFlux(rollupPowerFlux(seg, interval, src, g));
      // Rollups don't exist until the Pi-side backfill runs — degrade to raw
      // rather than render an empty chart.
      if (needsRawFallback(rows.length)) {
        return runPowerFlux(rawPowerFlux(seg, interval, g));
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

export type EnergyRow = {
  /** Category name, or — on a drill-down row — the circuit name. */
  category: string;
  /** Set on drill-down rows only: the category this circuit rolls up into.
   *  The table indents these under their parent's subtotal row (#12). */
  parent?: string;
  kwh: number;
  /** Same category's kWh in the immediately-preceding equal-length window. */
  prevKwh?: number;
  /** Window length in ms — carried on each row so the table can prorate the
   *  base charge without a separate prop (see web/lib/energyWindow.ts). */
  windowMs?: number;
};

/**
 * Integrate each circuit separately to Wh, derive category, then sum by
 * category. Regex after integral() keeps it cheap. integral() requires
 * _start/_stop in the group key.
 * Defensive: skip null/non-finite samples in raw data (one bad point can
 * make integral() emit a huge spurious value), and drop any per-circuit
 * integrals that come out negative (shouldn't happen after abs(), but has
 * been seen on wide ranges — likely sparse-data interpolation artifact).
 */
function rawEnergyFlux(seg: Segment, g: Grouping): string {
  return `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(seg.startMs)}, stop: ${fluxDate(seg.stopMs)})
  |> filter(fn: (r) => r._measurement == "circuit" and r._field == "power_w")
  |> filter(fn: (r) => exists r._value${groupingFilter(g)})
  ${absValue}
  |> group(columns: ["name", "_start", "_stop"])
  |> integral(unit: 1h)
  |> filter(fn: (r) => r._value >= 0.0)${energyGroupTail(g)}
`;
}

/** Collapse per-circuit Wh into the output series and convert to kWh. The
 *  drilled variant stops one level short of the category roll-up. */
function energyGroupTail(g: Grouping): string {
  const collapse =
    g.kind === "circuit"
      ? `
  |> group(columns: ["name"])`
      : `
  |> ${categoryFromNameFlux()}
  |> group(columns: ["category"])`;
  const keep = g.kind === "circuit" ? `"name"` : `"category"`;
  return `${collapse}
  |> sum()
  |> map(fn: (r) => ({ r with _value: r._value / 1000.0 }))
  |> keep(columns: [${keep}, "_value"])`;
}

/**
 * Sum stored per-bucket Wh. No integral, no re-aggregation — exact, and the
 * whole point of the rollups. As in the power path the stored timestamp is the
 * stop of the covered period, so the range is shifted forward one bucket; no
 * timeShift is needed because a sum ignores time.
 */
function rollupEnergyFlux(seg: Segment, src: EnergySource, g: Grouping): string {
  const off = rollupOffsetMs(src.bucketMs);
  return `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(seg.startMs + off)}, stop: ${fluxDate(seg.stopMs + off)})
  |> filter(fn: (r) => r._measurement == "${src.measurement}" and r._field == "${src.field}")
  |> filter(fn: (r) => exists r._value${groupingFilter(g)})
  |> filter(fn: (r) => r._value >= 0.0)${energyGroupTail(g)}
`;
}

/**
 * Whole-house grid energy (kWh) over [fromMs, toMs), via integral(grid_power_w).
 * No rollup exists for `panel` (unlike `circuit`, since #9) — this mirrors
 * pi/daily_report.py's query_daily_panel_kwh, which already runs the same raw
 * integral over windows up to 98 days in production without a perf problem.
 * Feeds the "Unmonitored" breakdown row (#17); returns 0 if the window has
 * no panel data rather than failing the whole breakdown.
 */
async function queryPanelKwh(fromMs: number, toMs: number): Promise<number> {
  const flux = `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(fromMs)}, stop: ${fluxDate(toMs)})
  |> filter(fn: (r) => r._measurement == "panel" and r._field == "grid_power_w")
  |> filter(fn: (r) => exists r._value)
  |> group(columns: ["_start", "_stop"])
  |> integral(unit: 1h)
`;
  const rows = await makeClient()
    .getQueryApi(ORG)
    .collectRows<{ _value: number }>(flux);
  return (rows[0]?._value ?? 0) / 1000.0;
}

async function runEnergyFlux(flux: string): Promise<EnergyRow[]> {
  const queryApi = makeClient().getQueryApi(ORG);
  const out: EnergyRow[] = [];
  for await (const row of queryApi.iterateRows(flux)) {
    const o = row.tableMeta.toObject(row.values) as Record<string, unknown>;
    out.push({
      // As in runPowerFlux: `category` when grouped by category, `name` when
      // drilled into one category's circuits.
      category: String(o.category ?? o.name ?? "Other"),
      kwh: Number(o._value) || 0,
    });
  }
  return out;
}

/**
 * Energy (kWh) per category across the range — one number per category.
 * Drives the breakdown table.
 *
 * Wide windows sum pre-computed `energy_wh_counter` from the rollups; the trailing
 * period the rollup task hasn't covered yet is integrated from raw, so a window
 * ending "now" reports today's energy in full rather than silently short.
 *
 * The category view (not a drill) also appends an "Unmonitored" row — the
 * panel's own grid total minus every named circuit, which is the Square D
 * overflow subpanel plus any metering slop (#17). Fetched concurrently with
 * the circuit segments so it costs no extra latency.
 */
export async function queryEnergyByCategory(opts: {
  fromMs: number;
  toMs: number;
  grouping?: Grouping;
  /** Injectable for tests; defaults to wall clock. */
  nowMs?: number;
}): Promise<EnergyRow[]> {
  const { fromMs, toMs } = opts;
  const g = opts.grouping ?? CATEGORY_GROUPING;
  const nowMs = opts.nowMs ?? Date.now();
  const src = energySourceForSpan(toMs - fromMs);
  const segments = planSegments({
    fromMs,
    toMs,
    nowMs,
    bucketMs: src.mode === "sum" ? src.bucketMs : RAW_ENERGY_SOURCE.bucketMs,
  });

  const [parts, panelKwh] = await Promise.all([
    Promise.all(
      segments.map(async (seg) => {
        if (seg.kind === "raw") return runEnergyFlux(rawEnergyFlux(seg, g));
        const rows = await runEnergyFlux(rollupEnergyFlux(seg, src, g));
        if (needsRawFallback(rows.length)) return runEnergyFlux(rawEnergyFlux(seg, g));
        return rows;
      }),
    ),
    g.kind === "circuit" ? Promise.resolve(0) : queryPanelKwh(fromMs, toMs),
  ]);

  const merged = mergeEnergyRows(parts.flat());
  // Drilled rows carry their parent so the table can nest them without
  // re-deriving membership client-side.
  if (g.kind === "circuit") {
    return merged.map((r) => ({ ...r, parent: g.category }));
  }
  const circuitKwh = merged.reduce((sum, r) => sum + r.kwh, 0);
  return [...merged, { category: "Unmonitored", kwh: unmonitoredKwh(panelKwh, circuitKwh) }];
}

/** Segments partition the window, so per-series totals add. */
export function mergeEnergyRows(rows: EnergyRow[]): EnergyRow[] {
  const byCat = new Map<string, number>();
  for (const r of rows) byCat.set(r.category, (byCat.get(r.category) ?? 0) + r.kwh);
  return Array.from(byCat, ([category, kwh]) => ({ category, kwh })).sort(
    (a, b) => b.kwh - a.kwh,
  );
}
