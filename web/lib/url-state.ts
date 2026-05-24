import {
  RANGE_PRESETS,
  type RangePreset,
  type IntervalKey,
  INTERVAL_ORDER,
  autoInterval,
  intervalSeconds,
} from "./interval";

export type DashState = {
  fromMs: number;
  toMs: number;
  interval: IntervalKey;
  intervalAuto: boolean;
  rangePreset: RangePreset | null;
  /** Subset of categories visible in the chart. Empty = show all.
   * Client-side only — does not affect Influx queries. */
  show: string[];
};

const isRangePreset = (v: string): v is RangePreset => v in RANGE_PRESETS;
const isInterval = (v: string): v is IntervalKey =>
  (INTERVAL_ORDER as string[]).includes(v);

const first = (v: string | string[] | undefined): string | undefined =>
  Array.isArray(v) ? v[0] : v;

function parseWindow(
  fromRaw: string | undefined,
  toRaw: string | undefined,
  range: string | undefined,
  now: number,
): { fromMs: number; toMs: number; rangePreset: RangePreset | null } {
  if (fromRaw && toRaw) {
    return { fromMs: Number(fromRaw), toMs: Number(toRaw), rangePreset: null };
  }
  const preset: RangePreset = range && isRangePreset(range) ? range : "24h";
  return { fromMs: now - RANGE_PRESETS[preset], toMs: now, rangePreset: preset };
}

function parseInterval(
  raw: string | undefined,
  fromMs: number,
  toMs: number,
): { interval: IntervalKey; intervalAuto: boolean } {
  const spanSec = Math.max(1, (toMs - fromMs) / 1000);
  if (raw && isInterval(raw) && intervalSeconds(raw) < spanSec) {
    return { interval: raw, intervalAuto: false };
  }
  return { interval: autoInterval(fromMs, toMs), intervalAuto: true };
}

const parseList = (raw: string | undefined): string[] =>
  raw ? raw.split(",").filter(Boolean) : [];

export function parseState(
  params: Record<string, string | string[] | undefined>,
): DashState {
  const now = Date.now();
  const win = parseWindow(
    first(params.from),
    first(params.to),
    first(params.range),
    now,
  );
  const iv = parseInterval(first(params.interval), win.fromMs, win.toMs);
  return {
    fromMs: win.fromMs,
    toMs: win.toMs,
    rangePreset: win.rangePreset,
    interval: iv.interval,
    intervalAuto: iv.intervalAuto,
    show: parseList(first(params.show)),
  };
}
