import { describe, it, expect } from "vitest";
import { TtlLru, cacheTtlMs } from "./clientCache";

const DAY = 24 * 60 * 60 * 1000;

describe("TtlLru", () => {
  it("returns undefined for a missing key", () => {
    const c = new TtlLru<number>(4);
    expect(c.get("nope", 0)).toBeUndefined();
  });

  it("stores and retrieves a value before expiry", () => {
    const c = new TtlLru<number>(4);
    c.set("a", 1, 100);
    expect(c.get("a", 50)).toBe(1);
  });

  it("treats an entry as gone once now passes expiresAt", () => {
    const c = new TtlLru<number>(4);
    c.set("a", 1, 100);
    expect(c.get("a", 100)).toBeUndefined();
    expect(c.get("a", 200)).toBeUndefined();
  });

  it("evicts the oldest entry past maxEntries", () => {
    const c = new TtlLru<number>(2);
    c.set("a", 1, DAY);
    c.set("b", 2, DAY);
    c.set("c", 3, DAY);
    expect(c.get("a", 0)).toBeUndefined(); // a evicted
    expect(c.get("b", 0)).toBe(2);
    expect(c.get("c", 0)).toBe(3);
  });

  it("a read bumps recency so the un-read entry is evicted first", () => {
    const c = new TtlLru<number>(2);
    c.set("a", 1, DAY);
    c.set("b", 2, DAY);
    c.get("a", 0); // bump a
    c.set("c", 3, DAY);
    expect(c.get("a", 0)).toBe(1); // survived
    expect(c.get("b", 0)).toBeUndefined(); // b evicted
  });

  it("overwrites an existing key without growing size", () => {
    const c = new TtlLru<number>(2);
    c.set("a", 1, DAY);
    c.set("a", 9, DAY);
    expect(c.get("a", 0)).toBe(9);
    expect(c.size).toBe(1);
  });
});

describe("cacheTtlMs", () => {
  const HOUR_MS = 60 * 60 * 1000;

  it("gives a short ttl for a trailing window (to ~ now)", () => {
    const now = 10 * DAY;
    // 15m bucket, window ends at now
    const ttl = cacheTtlMs(now, 15 * 60 * 1000, now);
    expect(ttl).toBe(15 * 60 * 1000); // one bucket
  });

  it("floors the trailing ttl at 60s for tiny buckets", () => {
    const now = 10 * DAY;
    const ttl = cacheTtlMs(now, 30 * 1000, now); // 30s bucket
    expect(ttl).toBe(60_000);
  });

  it("gives 24h for a historical window", () => {
    const now = 10 * DAY;
    const toMs = now - 5 * DAY; // well in the past
    expect(cacheTtlMs(toMs, HOUR_MS, now)).toBe(DAY);
  });
});
