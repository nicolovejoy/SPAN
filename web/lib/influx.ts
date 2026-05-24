import { InfluxDB } from "@influxdata/influxdb-client";
import type { IntervalKey } from "./interval";
import { fluxEvery } from "./interval";
import { categoryFromNameFlux } from "./categories";

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

const POWER_FILTER = `r._measurement == "circuit" and r._field == "power_w"`;

/**
 * Mean power per bucket, grouped by derived category. One row per
 * (time, category) — pivot client-side.
 */
export async function queryPower(opts: {
  fromMs: number;
  toMs: number;
  interval: IntervalKey;
}): Promise<SeriesPoint[]> {
  const { fromMs, toMs, interval } = opts;
  const queryApi = makeClient().getQueryApi(ORG);

  // Per-circuit aggregateWindow first, then derive category from name (so
  // historical rows written before category was tagged still group correctly),
  // then sum across circuits per category at each bucket boundary. The regex
  // runs on ~30×buckets rows instead of every raw point.
  // createEmpty: false — emitting synthetic rows for empty buckets caused
  // lightweight-charts to throw "Value is null" in its line renderer (it
  // assumes contiguous numeric data and our synthetic-row pipeline must
  // have been leaving a column null). We backfill gaps client-side instead.
  const flux = `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(fromMs)}, stop: ${fluxDate(toMs)})
  |> filter(fn: (r) => ${POWER_FILTER})
  |> filter(fn: (r) => exists r._value)
  |> map(fn: (r) => ({ r with _value: if r._value < 0.0 then -r._value else r._value }))
  |> aggregateWindow(every: ${fluxEvery(interval)}, fn: mean, createEmpty: false)
  |> ${categoryFromNameFlux()}
  |> group(columns: ["_time", "category"])
  |> sum()
  |> group(columns: ["category"])
  |> keep(columns: ["_time", "_value", "category"])
`;

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
 * Energy (kWh) per category across the range — one number per category.
 * Drives the breakdown table.
 */
export async function queryEnergyByCategory(opts: {
  fromMs: number;
  toMs: number;
}): Promise<Array<{ category: string; kwh: number }>> {
  const { fromMs, toMs } = opts;
  const queryApi = makeClient().getQueryApi(ORG);

  // Integrate each circuit separately to Wh, derive category, then sum by
  // category. Regex after integral() keeps it cheap. integral() requires
  // _start/_stop in the group key.
  // Defensive: skip null/non-finite samples in raw data (one bad point can
  // make integral() emit a huge spurious value), and drop any per-circuit
  // integrals that come out negative (shouldn't happen after abs(), but has
  // been seen on wide ranges — likely sparse-data interpolation artifact).
  const flux = `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(fromMs)}, stop: ${fluxDate(toMs)})
  |> filter(fn: (r) => ${POWER_FILTER})
  |> filter(fn: (r) => exists r._value)
  |> map(fn: (r) => ({ r with _value: if r._value < 0.0 then -r._value else r._value }))
  |> group(columns: ["name", "_start", "_stop"])
  |> integral(unit: 1h)
  |> filter(fn: (r) => r._value >= 0.0)
  |> ${categoryFromNameFlux()}
  |> group(columns: ["category"])
  |> sum()
  |> map(fn: (r) => ({ r with _value: r._value / 1000.0 }))
  |> keep(columns: ["category", "_value"])
`;

  const out: Array<{ category: string; kwh: number }> = [];
  for await (const row of queryApi.iterateRows(flux)) {
    const o = row.tableMeta.toObject(row.values) as Record<string, unknown>;
    out.push({
      category: String(o.category ?? "Other"),
      kwh: Number(o._value) || 0,
    });
  }
  return out.sort((a, b) => b.kwh - a.kwh);
}
