export type IntervalKey = "1m" | "5m" | "15m" | "1h" | "6h" | "1d" | "1w";

const SECONDS: Record<IntervalKey, number> = {
  "1m": 60,
  "5m": 5 * 60,
  "15m": 15 * 60,
  "1h": 60 * 60,
  "6h": 6 * 60 * 60,
  "1d": 24 * 60 * 60,
  "1w": 7 * 24 * 60 * 60,
};

export const INTERVAL_ORDER: IntervalKey[] = [
  "1m",
  "5m",
  "15m",
  "1h",
  "6h",
  "1d",
  "1w",
];

const TARGET_POINTS = 175;

/**
 * Pick the smallest interval that produces at most TARGET_POINTS buckets
 * across [from, to]. If no interval is small enough, returns "1w".
 */
export function autoInterval(fromMs: number, toMs: number): IntervalKey {
  const spanSec = Math.max(1, (toMs - fromMs) / 1000);
  for (const key of INTERVAL_ORDER) {
    if (spanSec / SECONDS[key] <= TARGET_POINTS) return key;
  }
  return "1w";
}

export function intervalSeconds(key: IntervalKey): number {
  return SECONDS[key];
}

export function fluxEvery(key: IntervalKey): string {
  return key;
}

export type RangePreset =
  | "1h"
  | "6h"
  | "24h"
  | "7d"
  | "30d"
  | "90d"
  | "1y";

export const RANGE_PRESETS: Record<RangePreset, number> = {
  "1h": 60 * 60 * 1000,
  "6h": 6 * 60 * 60 * 1000,
  "24h": 24 * 60 * 60 * 1000,
  "7d": 7 * 24 * 60 * 60 * 1000,
  "30d": 30 * 24 * 60 * 60 * 1000,
  "90d": 90 * 24 * 60 * 60 * 1000,
  "1y": 365 * 24 * 60 * 60 * 1000,
};
