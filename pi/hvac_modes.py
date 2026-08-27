#!/usr/bin/env python3
"""Pure HVAC mode classification — no I/O, no env, no influx imports.

Turns raw 30s HP + aux power samples plus hourly outdoor temperature into a
labeled 5-minute timeline: heat / cool / hot_water / idle / ambiguous.
Thresholds below are Phase 0 SEEDS; see the findings note referenced next to
each constant once Task 5 has tuned them against January-to-now data.
"""
from datetime import datetime, timedelta, timezone

INTERVAL_MINUTES = 5
IDLE_POWER_W = 50.0            # on/off boundary, same as bath_detector's POWER_THRESHOLD

# --- DHW (hot water) signature seeds — season-invariant, Phase 0-tunable ---
DHW_MEAN_POWER_MIN_W = 2500.0  # seeded from bath_detector.MEAN_POWER_MIN
DHW_DUTY_MIN = 0.85            # seeded from bath_detector.DUTY_CYCLE_MIN
DHW_MAX_TRANSITIONS = 2        # seeded from bath_detector.MAX_TRANSITIONS
DHW_RUN_MIN_MINUTES = 10       # shorter high-power runs -> not a reheat
DHW_RUN_MAX_MINUTES = 120      # longer -> sustained space conditioning

# --- heat vs cool temperature bands (deg F), Phase 0-tunable ---
HEAT_MAX_TEMP_F = 58.0
COOL_MIN_TEMP_F = 68.0
WEATHER_MAX_STALENESS_MIN = 90


def _floor_to_interval(dt: datetime) -> datetime:
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - epoch % (INTERVAL_MINUTES * 60), tz=timezone.utc)


def _stats(powers: list[float]) -> dict:
    above = [p > IDLE_POWER_W for p in powers]
    transitions = sum(1 for i in range(1, len(above)) if above[i] != above[i - 1])
    return {
        "mean": sum(powers) / len(powers) if powers else 0.0,
        "max": max(powers) if powers else 0.0,
        "duty": sum(above) / len(above) if above else 0.0,
        "transitions": transitions,
    }


def bucket_intervals(hp_samples: list[dict], aux_samples: list[dict],
                     start: datetime, stop: datetime) -> list[dict]:
    """Aggregate raw samples into aligned 5-min interval stats. Intervals with
    zero HP samples are omitted: a collector gap must surface as a timeline
    gap, never as invented zeros."""
    width = timedelta(minutes=INTERVAL_MINUTES)
    hp_by_bucket: dict[datetime, list[float]] = {}
    aux_by_bucket: dict[datetime, list[float]] = {}
    for target, source in ((hp_by_bucket, hp_samples), (aux_by_bucket, aux_samples)):
        for s in source:
            if start <= s["time"] < stop:
                target.setdefault(_floor_to_interval(s["time"]), []).append(abs(s["power"]))

    out = []
    b = _floor_to_interval(start)
    while b < stop:
        hp = hp_by_bucket.get(b, [])
        if hp:
            h, a = _stats(hp), _stats(aux_by_bucket.get(b, []))
            out.append({
                "start": b,
                "hp_mean_w": h["mean"], "hp_max_w": h["max"],
                "hp_duty": h["duty"], "hp_transitions": h["transitions"],
                "aux_mean_w": a["mean"], "aux_max_w": a["max"],
                "energy_kwh": (h["mean"] + a["mean"]) * INTERVAL_MINUTES / 60 / 1000,
                "n_samples": len(hp),
            })
        b += width
    return out
