import type { GroupBy } from "./influx";
import {
  RANGE_PRESETS,
  type RangePreset,
  type IntervalKey,
  INTERVAL_ORDER,
  autoInterval,
} from "./interval";

export type DashState = {
  fromMs: number;
  toMs: number;
  interval: IntervalKey;
  intervalAuto: boolean;
  groupBy: GroupBy;
  categories: string[];
  rangePreset: RangePreset | null;
};

const GROUP_BYS: GroupBy[] = ["all", "category", "circuit"];
const isRangePreset = (v: string): v is RangePreset =>
  v in RANGE_PRESETS;
const isInterval = (v: string): v is IntervalKey =>
  (INTERVAL_ORDER as string[]).includes(v);
const isGroupBy = (v: string): v is GroupBy =>
  (GROUP_BYS as string[]).includes(v);

export function parseState(
  params: Record<string, string | string[] | undefined>
): DashState {
  const get = (k: string) => {
    const v = params[k];
    return Array.isArray(v) ? v[0] : v;
  };

  const now = Date.now();

  let fromMs: number;
  let toMs: number;
  let rangePreset: RangePreset | null = null;
  const fromRaw = get("from");
  const toRaw = get("to");
  const range = get("range");

  if (fromRaw && toRaw) {
    fromMs = Number(fromRaw);
    toMs = Number(toRaw);
  } else if (range && isRangePreset(range)) {
    rangePreset = range;
    toMs = now;
    fromMs = now - RANGE_PRESETS[range];
  } else {
    rangePreset = "24h";
    toMs = now;
    fromMs = now - RANGE_PRESETS["24h"];
  }

  const intervalRaw = get("interval");
  let interval: IntervalKey;
  let intervalAuto: boolean;
  if (intervalRaw && isInterval(intervalRaw)) {
    interval = intervalRaw;
    intervalAuto = false;
  } else {
    interval = autoInterval(fromMs, toMs);
    intervalAuto = true;
  }

  const groupRaw = get("groupBy");
  const groupBy: GroupBy = groupRaw && isGroupBy(groupRaw) ? groupRaw : "category";

  const catsRaw = get("categories");
  const categories = catsRaw ? catsRaw.split(",").filter(Boolean) : [];

  return { fromMs, toMs, interval, intervalAuto, groupBy, categories, rangePreset };
}

export function serializeState(s: Partial<DashState>): URLSearchParams {
  const out = new URLSearchParams();
  if (s.rangePreset) {
    out.set("range", s.rangePreset);
  } else if (s.fromMs && s.toMs) {
    out.set("from", String(s.fromMs));
    out.set("to", String(s.toMs));
  }
  if (s.interval && !s.intervalAuto) out.set("interval", s.interval);
  if (s.groupBy && s.groupBy !== "category") out.set("groupBy", s.groupBy);
  if (s.categories?.length) out.set("categories", s.categories.join(","));
  return out;
}
