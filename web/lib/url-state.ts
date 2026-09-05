import {
  RANGE_PRESETS,
  type RangePreset,
  type IntervalKey,
  INTERVAL_ORDER,
  autoInterval,
  intervalSeconds,
  isIntervalAllowed,
} from "./interval";
import { isCategory } from "./categories";

export type DashState = {
  fromMs: number;
  toMs: number;
  interval: IntervalKey;
  intervalAuto: boolean;
  rangePreset: RangePreset | null;
  /** Subset of categories visible in the chart. Empty = show all.
   * Client-side only — does not affect Influx queries. */
  show: string[];
  /** Category expanded into its member circuits, or null. At most one at a
   * time (#12). Unlike `show` this *does* drive a query. */
  drill: string | null;
  /** Events layer (mode strip + list) visible. Default true; URL carries
   *  events=0 only when off. */
  events: boolean;
};

/**
 * Build the intent-only search string for the URL: the preset range, the
 * visible-category filter, and the drilled category — never the transient
 * pan/zoom window. Commas in `show` are kept literal (URLSearchParams would
 * emit %2C) for a readable URL.
 */
export function buildIntentSearch(
  range: RangePreset | null,
  show: string[],
  drill: string | null = null,
  events: boolean = true,
): string {
  const parts: string[] = [];
  if (range) parts.push(`range=${range}`);
  if (show.length) parts.push(`show=${show.join(",")}`);
  if (drill) parts.push(`drill=${drill}`);
  if (!events) parts.push("events=0");
  return parts.join("&");
}

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
  if (
    raw &&
    isInterval(raw) &&
    intervalSeconds(raw) < spanSec &&
    isIntervalAllowed(raw, fromMs, toMs)
  ) {
    return { interval: raw, intervalAuto: false };
  }
  return { interval: autoInterval(fromMs, toMs), intervalAuto: true };
}

const parseList = (raw: string | undefined): string[] =>
  raw ? raw.split(",").filter(Boolean) : [];

/** A drill only survives a reload if it names a real category and that category
 *  is actually visible under the current `show` filter — otherwise the chart
 *  would drill into a hidden line. */
export function parseDrill(
  raw: string | undefined,
  show: string[],
): string | null {
  if (!raw || !isCategory(raw)) return null;
  if (show.length && !show.includes(raw)) return null;
  return raw;
}

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
  const show = parseList(first(params.show));
  const drill = parseDrill(first(params.drill), show);
  return {
    fromMs: win.fromMs,
    toMs: win.toMs,
    rangePreset: win.rangePreset,
    interval: iv.interval,
    intervalAuto: iv.intervalAuto,
    // A drill focuses `show` on the drilled category (ExplorerClient's reducer
    // does the same on click) so a small family of circuits gets the price
    // scale to itself. The URL therefore always carries the *narrowed* show —
    // it describes what's on screen, not what it was before the drill. The
    // pre-drill filter is reducer-only state and doesn't survive a reload, so
    // backing out of a drill you arrived at by URL leaves `show` narrowed.
    show: drill ? [drill] : show,
    drill,
    events: first(params.events) !== "0",
  };
}
