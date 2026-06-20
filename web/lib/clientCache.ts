// Client-side time-aware LRU. Mirrors the server `queryCache` semantics in the
// browser so back-and-forth between visited windows is a 0-network cache hit.
// `now` is passed in (not read from Date.now) to keep the cache pure/testable.

type Entry<V> = { value: V; expiresAt: number };

export class TtlLru<V> {
  private readonly max: number;
  private readonly map = new Map<string, Entry<V>>();

  constructor(maxEntries: number) {
    this.max = Math.max(1, maxEntries);
  }

  get(key: string, now: number): V | undefined {
    const hit = this.map.get(key);
    if (!hit) return undefined;
    if (now >= hit.expiresAt) {
      this.map.delete(key);
      return undefined;
    }
    // LRU bump — re-insert so it becomes newest in Map iteration order.
    this.map.delete(key);
    this.map.set(key, hit);
    return hit.value;
  }

  set(key: string, value: V, expiresAt: number): void {
    this.map.delete(key); // ensure overwrite refreshes recency + doesn't double-count
    this.map.set(key, { value, expiresAt });
    while (this.map.size > this.max) {
      const oldest = this.map.keys().next().value;
      if (oldest === undefined) break;
      this.map.delete(oldest);
    }
  }

  get size(): number {
    return this.map.size;
  }
}

// TTL for a fetched window. A trailing window (ends ~now) keeps changing as new
// data lands, so cap it at one bucket (floored at 60s). A historical window is
// immutable → 24h.
export function cacheTtlMs(toMs: number, intervalMs: number, now: number): number {
  const isTrailing = now - toMs < 2 * intervalMs;
  return isTrailing ? Math.max(60_000, intervalMs) : 24 * 60 * 60 * 1000;
}
