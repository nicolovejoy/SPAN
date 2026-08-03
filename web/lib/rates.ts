import { DAY_MS } from "./rollup";

/**
 * Flat energy rate model — Seattle City Light "Small General Energy" schedule
 * (per the May 2026 bill: $0.1241/kWh + $0.83/day base charge). No TOU/tiered
 * split modeled here; plan confirmation is still open (see CLAUDE.md Next
 * Steps), and `pi/rates.py` — the daily report's rate model — is the
 * source of truth to keep in sync with if the plan changes.
 */
export const ENERGY_RATE_PER_KWH = 0.1241; // $/kWh
export const BASE_CHARGE_PER_DAY = 0.83; // $/day fixed service charge

/** Energy cost in dollars for the given kWh at the flat rate. */
export function costForKwh(kwh: number): number {
  return kwh * ENERGY_RATE_PER_KWH;
}

/** Fixed service charge prorated over an arbitrary window length. */
export function proratedBaseCharge(windowMs: number): number {
  return (windowMs / DAY_MS) * BASE_CHARGE_PER_DAY;
}
