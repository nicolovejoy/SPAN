import { describe, expect, it } from "vitest";
import { evaluateCheck, HEALTH_CHECKS } from "./health";

const now = new Date("2026-08-13T12:00:00Z");

describe("evaluateCheck", () => {
  it("passes a fresh point", () => {
    const c = evaluateCheck("collector", "2026-08-13T11:59:30Z", now, 300);
    expect(c.ok).toBe(true);
    expect(c.ageSeconds).toBe(30);
  });

  it("passes exactly at the threshold", () => {
    const c = evaluateCheck("collector", "2026-08-13T11:55:00Z", now, 300);
    expect(c.ok).toBe(true);
    expect(c.ageSeconds).toBe(300);
  });

  it("fails a stale point", () => {
    const c = evaluateCheck("collector", "2026-08-13T11:54:59Z", now, 300);
    expect(c.ok).toBe(false);
    expect(c.ageSeconds).toBe(301);
  });

  it("fails when no point exists", () => {
    const c = evaluateCheck("backup", null, now, 300);
    expect(c.ok).toBe(false);
    expect(c.ageSeconds).toBeNull();
    expect(c.note).toMatch(/no data point/);
  });

  it("fails on an unparseable timestamp", () => {
    const c = evaluateCheck("backup", "not-a-date", now, 300);
    expect(c.ok).toBe(false);
    expect(c.note).toMatch(/unparseable/);
  });

  it("clamps future timestamps to age 0", () => {
    const c = evaluateCheck("collector", "2026-08-13T12:01:00Z", now, 300);
    expect(c.ok).toBe(true);
    expect(c.ageSeconds).toBe(0);
  });

  it("handles Influx-style nanosecond timestamps", () => {
    const c = evaluateCheck(
      "collector",
      "2026-08-13T11:59:00.243384Z",
      now,
      300,
    );
    expect(c.ok).toBe(true);
    expect(c.ageSeconds).toBe(60);
  });
});

describe("HEALTH_CHECKS registry", () => {
  it("names the four cadence-bound services once each", () => {
    expect(HEALTH_CHECKS.map((c) => c.name)).toEqual([
      "collector",
      "backup",
      "weather",
      "hvac_mode",
    ]);
  });

  it("thresholds are 3x each service's cadence or more", () => {
    const by = Object.fromEntries(HEALTH_CHECKS.map((c) => [c.name, c]));
    expect(by.collector.maxAgeSeconds).toBe(300);        // 30s poll
    expect(by.backup.maxAgeSeconds).toBe(30 * 3600);     // nightly
    expect(by.weather.maxAgeSeconds).toBe(3 * 3600);     // hourly poll
    expect(by.hvac_mode.maxAgeSeconds).toBe(45 * 60);    // 10-min loop, 5-min intervals
  });

  it("every check has a measurement, field and lookback", () => {
    for (const c of HEALTH_CHECKS) {
      expect(c.measurement).toBeTruthy();
      expect(c.field).toBeTruthy();
      expect(c.lookback).toMatch(/^\d+[hd]$/);
    }
  });
});
