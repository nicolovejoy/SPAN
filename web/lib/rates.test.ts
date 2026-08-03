import { describe, it, expect } from "vitest";
import {
  BASE_CHARGE_PER_DAY,
  ENERGY_RATE_PER_KWH,
  costForKwh,
  proratedBaseCharge,
} from "./rates";
import { DAY_MS } from "./rollup";

describe("costForKwh", () => {
  it("multiplies by the flat rate", () => {
    expect(costForKwh(10)).toBeCloseTo(10 * ENERGY_RATE_PER_KWH);
  });

  it("is zero for zero kWh", () => {
    expect(costForKwh(0)).toBe(0);
  });
});

describe("proratedBaseCharge", () => {
  it("returns the full daily charge for a 1-day window", () => {
    expect(proratedBaseCharge(DAY_MS)).toBeCloseTo(BASE_CHARGE_PER_DAY);
  });

  it("prorates down for sub-day windows", () => {
    expect(proratedBaseCharge(DAY_MS / 2)).toBeCloseTo(BASE_CHARGE_PER_DAY / 2);
  });

  it("scales up for multi-day windows", () => {
    expect(proratedBaseCharge(7 * DAY_MS)).toBeCloseTo(7 * BASE_CHARGE_PER_DAY);
  });
});
