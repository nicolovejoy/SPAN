import { queryPower, type SeriesPoint } from "./influx";
import { intervalSeconds, type IntervalKey } from "./interval";

// In-memory LRU keyed by (interval, quantized from, quantized to). Lives in
// the Node server module scope — survives across requests on the Pi (where
// the process is long-lived, not serverless). Past buckets are immutable so
// historical entries get a 24h TTL; trailing-bucket entries TTL out after
// one bucket so newly-arrived data shows up promptly.

type Entry = { data: SeriesPoint[]; expiresAt: number };

const MAX_ENTRIES = 100;
const cache = new Map<string, Entry>();
const inflight = new Map<string, Promise<SeriesPoint[]>>();

function makeKey(interval: IntervalKey, fromMs: number, toMs: number): string {
  return `${interval}|${fromMs}|${toMs}`;
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
