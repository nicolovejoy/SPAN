import { queryPower, queryEnergyByCategory, type SeriesPoint } from "./influx";
import { intervalSeconds, type IntervalKey } from "./interval";
import { energySourceForSpan, sourceForInterval, sourceKey } from "./rollup";

export type { EnergyRow } from "./influx";
import type { EnergyRow } from "./influx";

// In-memory LRU keyed by (interval, quantized from, quantized to). Lives in
// the Node server module scope — survives across requests on the Pi (where
// the process is long-lived, not serverless). Past buckets are immutable so
// historical entries get a 24h TTL; trailing-bucket entries TTL out after
// one bucket so newly-arrived data shows up promptly.

type Entry = { data: SeriesPoint[]; expiresAt: number };

const MAX_ENTRIES = 100;
const cache = new Map<string, Entry>();
const inflight = new Map<string, Promise<SeriesPoint[]>>();

// The measurement is in the key so an entry can never be served from a
// different source than it was written with (the source is a pure function of
// the interval today, so this is belt-and-braces against a threshold change).
function makeKey(interval: IntervalKey, fromMs: number, toMs: number): string {
  return `${interval}|${sourceKey(sourceForInterval(interval))}|${fromMs}|${toMs}`;
}

export async function cachedQueryPower(opts: {
  fromMs: number;
  toMs: number;
  interval: IntervalKey;
}): Promise<SeriesPoint[]> {
  const key = makeKey(opts.interval, opts.fromMs, opts.toMs);
  const now = Date.now();

  const hit = cache.get(key);
  if (hit && hit.expiresAt > now) {
    // LRU bump — re-insert so this entry moves to newest in Map iteration order.
    cache.delete(key);
    cache.set(key, hit);
    return hit.data;
  }

  // Coalesce concurrent identical requests.
  const pending = inflight.get(key);
  if (pending) return pending;

  const intervalMs = intervalSeconds(opts.interval) * 1000;
  const isTrailing = now - opts.toMs < 2 * intervalMs;
  const ttlMs = isTrailing ? Math.max(60_000, intervalMs) : 24 * 60 * 60 * 1000;

  const promise = queryPower(opts)
    .then((data) => {
      cache.set(key, { data, expiresAt: Date.now() + ttlMs });
      while (cache.size > MAX_ENTRIES) {
        const oldest = cache.keys().next().value;
        if (oldest === undefined) break;
        cache.delete(oldest);
      }
      inflight.delete(key);
      return data;
    })
    .catch((e) => {
      inflight.delete(key);
      throw e;
    });

  inflight.set(key, promise);
  return promise;
}

// Energy-by-category cache. Keyed by (from, to) only — energy is the integral
// over the whole window, independent of the display bucket, so switching the
// chart's interval must not invalidate the table.
const energyCache = new Map<string, { data: EnergyRow[]; expiresAt: number }>();
const energyInflight = new Map<string, Promise<EnergyRow[]>>();

export async function cachedQueryEnergyByCategory(opts: {
  fromMs: number;
  toMs: number;
}): Promise<EnergyRow[]> {
  // Source is derived from the span, which the key already pins — included
  // explicitly so a stale entry can't outlive a threshold change.
  const key = `${sourceKey(energySourceForSpan(opts.toMs - opts.fromMs))}|${opts.fromMs}|${opts.toMs}`;
  const now = Date.now();

  const hit = energyCache.get(key);
  if (hit && hit.expiresAt > now) {
    energyCache.delete(key);
    energyCache.set(key, hit);
    return hit.data;
  }

  const pending = energyInflight.get(key);
  if (pending) return pending;

  // Trailing window (ends ~now) keeps changing as 30s data lands → short TTL;
  // historical is immutable → 24h.
  const isTrailing = now - opts.toMs < 2 * 60_000;
  const ttlMs = isTrailing ? 60_000 : 24 * 60 * 60 * 1000;

  const promise = queryEnergyByCategory(opts)
    .then((data) => {
      energyCache.set(key, { data, expiresAt: Date.now() + ttlMs });
      while (energyCache.size > MAX_ENTRIES) {
        const oldest = energyCache.keys().next().value;
        if (oldest === undefined) break;
        energyCache.delete(oldest);
      }
      energyInflight.delete(key);
      return data;
    })
    .catch((e) => {
      energyInflight.delete(key);
      throw e;
    });

  energyInflight.set(key, promise);
  return promise;
}
