#!/usr/bin/env python3
"""Pure run-grouping over a classified hvac_mode timeline — no I/O.

An event detector is a predicate over runs. One ships today (bath);
shower / laundry hot-water predicates are future one-liners here (#14/#17)."""
from datetime import timedelta

from hvac_modes import INTERVAL_MINUTES
from rates import cost_for_kwh

BATH_MIN_MINUTES = 25          # bath_detector required >= 3 overlapping 15-min windows
BATH_MEAN_POWER_MIN_W = 2500.0


def runs(intervals: list[dict], mode: str) -> list[list[dict]]:
    """Maximal groups of contiguous (exactly INTERVAL_MINUTES apart)
    intervals labeled `mode`. A timeline gap breaks a run."""
    out: list[list[dict]] = []
    current: list[dict] = []
    step = timedelta(minutes=INTERVAL_MINUTES)
    for i in intervals:
        if i["mode"] != mode:
            if current:
                out.append(current)
                current = []
            continue
        if current and i["start"] - current[-1]["start"] != step:
            out.append(current)
            current = []
        current.append(i)
    if current:
        out.append(current)
    return out


def bath_events(intervals: list[dict]) -> list[dict]:
    """hot_water runs meeting duration + power bounds, shaped exactly like
    bath_detector's historical event dicts so write_bath_event and the ±2h
    dedup keep working unchanged."""
    events = []
    for run in runs(intervals, "hot_water"):
        duration_min = len(run) * INTERVAL_MINUTES
        hp_mean = sum(i["hp_mean_w"] for i in run) / len(run)
        if duration_min < BATH_MIN_MINUTES or hp_mean < BATH_MEAN_POWER_MIN_W:
            continue
        start = run[0]["start"]
        end = run[-1]["start"] + timedelta(minutes=INTERVAL_MINUTES)
        aux_mean = sum(i["aux_mean_w"] for i in run) / len(run)
        energy_kwh = sum(i["energy_kwh"] for i in run)
        events.append({
            "start": start,
            "end": end,
            "duration_min": float(duration_min),
            "hp_mean_power_w": round(hp_mean, 1),
            "hp_max_power_w": round(max(i["hp_max_w"] for i in run), 1),
            "aux_active": any(i["aux_mean_w"] > 50.0 for i in run),
            "aux_mean_power_w": round(aux_mean, 1),
            "aux_max_power_w": round(max(i["aux_max_w"] for i in run), 1),
            "energy_kwh": round(energy_kwh, 3),
            "cost_dollars": round(cost_for_kwh(energy_kwh, start), 2),
        })
    return events
