#!/usr/bin/env python3
"""Energy rate model.

Seattle City Light, Small General Service schedule (per May 2026 bill).
Flat $/kWh — no TOU peak/off-peak split, no seasonal variation.

If the service ever moves to a TOU schedule, reintroduce a get_rate(dt)
that branches on hour/season; callers already pass a datetime.
"""

from datetime import datetime

ENERGY_RATE = 0.1241        # $/kWh (Apr 2026+; pre-Apr was 0.1291)
BASE_CHARGE_DAILY = 0.83    # $/day fixed service charge (≈$47.31 / 57 days)


def get_rate(_dt: datetime) -> float:
    """Return $/kWh rate. Flat — datetime is accepted for API compatibility."""
    return ENERGY_RATE


def cost_for_kwh(kwh: float, dt: datetime) -> float:
    """Energy cost in dollars for the given kWh."""
    return kwh * get_rate(dt)


def is_peak(_dt: datetime) -> bool:
    """No TOU on this plan."""
    return False
