#!/usr/bin/env python3
"""TOU rate schedule for cost calculations.

Update these values to match your actual utility rates.
Default: approximate PG&E E-TOU-C schedule.
"""

from datetime import datetime

# PG&E E-TOU-C (placeholder — update with your actual rates)
# Summer: June 1 - September 30
# Winter: October 1 - May 31
# Peak: 4pm - 9pm weekdays
RATES = {
    "summer": {
        "peak": 0.54,       # $/kWh
        "off_peak": 0.40,
    },
    "winter": {
        "peak": 0.48,
        "off_peak": 0.39,
    },
}


def is_summer(dt: datetime) -> bool:
    return 6 <= dt.month <= 9


def is_peak(dt: datetime) -> bool:
    return 16 <= dt.hour < 21 and dt.weekday() < 5


def get_rate(dt: datetime) -> float:
    """Get $/kWh rate for the given datetime."""
    season = "summer" if is_summer(dt) else "winter"
    period = "peak" if is_peak(dt) else "off_peak"
    return RATES[season][period]


def cost_for_kwh(kwh: float, dt: datetime) -> float:
    """Calculate cost in dollars for given kWh at the given time."""
    return kwh * get_rate(dt)
