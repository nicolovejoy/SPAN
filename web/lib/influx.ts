import { InfluxDB } from "@influxdata/influxdb-client";
import type { IntervalKey } from "./interval";
import { fluxEvery } from "./interval";

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
    headers:
      CF_ID && CF_SECRET
        ? {
            "CF-Access-Client-Id": CF_ID,
            "CF-Access-Client-Secret": CF_SECRET,
          }
        : undefined,
  });
}

export type GroupBy = "all" | "category" | "circuit";

export type SeriesPoint = { time: string; series: string; watts: number };

const fluxDate = (ms: number) => new Date(ms).toISOString();

/**
 * Query mean power per bucket grouped by the chosen dimension.
 * Returns one row per (time, series) — easy to pivot client-side.
 */
export async function queryPower(opts: {
  fromMs: number;
  toMs: number;
  interval: IntervalKey;
  groupBy: GroupBy;
  categories?: string[];
  circuits?: string[];
}): Promise<SeriesPoint[]> {
  const { fromMs, toMs, interval, groupBy, categories, circuits } = opts;
  const client = makeClient();
  const queryApi = client.getQueryApi(ORG);

  const filters: string[] = [
    `r._measurement == "circuit"`,
    `r._field == "power_w"`,
  ];
  if (categories?.length) {
    const set = categories.map((c) => `r.category == "${c}"`).join(" or ");
    filters.push(`(${set})`);
  }
  if (circuits?.length) {
    const set = circuits.map((c) => `r.name == "${c}"`).join(" or ");
    filters.push(`(${set})`);
  }

  const groupCol =
    groupBy === "all" ? null : groupBy === "category" ? "category" : "name";

  // Strategy: per-circuit aggregateWindow first (mean watts in each bucket
  // per circuit), then sum across circuits within the chosen group at each
  // bucket boundary. This gives total instantaneous power for the group.
  const flux = `
import "math"

from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(fromMs)}, stop: ${fluxDate(toMs)})
  |> filter(fn: (r) => ${filters.join(" and ")})
  |> map(fn: (r) => ({ r with _value: if r._value < 0.0 then -r._value else r._value }))
  |> aggregateWindow(every: ${fluxEvery(interval)}, fn: mean, createEmpty: false)
  |> fill(value: 0.0)
  ${
    groupCol
      ? `|> group(columns: ["_time", "${groupCol}"])
  |> sum()
  |> group(columns: ["${groupCol}"])`
      : `|> group(columns: ["_time"])
  |> sum()
  |> group()`
  }
  |> keep(columns: ["_time", "_value"${groupCol ? `, "${groupCol}"` : ""}])
`;

  const out: SeriesPoint[] = [];
  for await (const row of queryApi.iterateRows(flux)) {
    const o = row.tableMeta.toObject(row.values) as Record<string, unknown>;
    out.push({
      time: String(o._time),
      series: groupCol ? String(o[groupCol] ?? "Other") : "Total",
      watts: Number(o._value) || 0,
    });
  }
  return out;
}

/**
 * Energy (kWh) per series across the entire range — one number per series.
 * Used for the breakdown table.
 */
export async function queryEnergyByCategory(opts: {
  fromMs: number;
  toMs: number;
  categories?: string[];
}): Promise<Array<{ category: string; kwh: number }>> {
  const { fromMs, toMs, categories } = opts;
  const client = makeClient();
  const queryApi = client.getQueryApi(ORG);

  const filters = [
    `r._measurement == "circuit"`,
    `r._field == "power_w"`,
  ];
  if (categories?.length) {
    const set = categories.map((c) => `r.category == "${c}"`).join(" or ");
    filters.push(`(${set})`);
  }

  // Integrate each circuit's power separately to get Wh, then sum by
  // category. integral() requires _start/_stop in the group key.
  const flux = `
from(bucket: "${BUCKET}")
  |> range(start: ${fluxDate(fromMs)}, stop: ${fluxDate(toMs)})
  |> filter(fn: (r) => ${filters.join(" and ")})
  |> map(fn: (r) => ({ r with _value: if r._value < 0.0 then -r._value else r._value }))
  |> group(columns: ["name", "category", "_start", "_stop"])
  |> integral(unit: 1h)
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
