#!/usr/bin/env python3
"""Pure baseline math for the daily anomaly check: median/MAD, weekday
bucketing, and the anomaly trigger. No Influx, no email — daily_report.py owns
all I/O and calls into this module. See
docs/superpowers/specs/2026-08-21-weekly-energy-report-design.md ("The anomaly
email")."""

from dataclasses import dataclass
from datetime import date, timedelta


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def mad(values: list[float], m: float | None = None) -> float:
    """Median absolute deviation, scaled by 1.4826 so it's comparable to a
    standard deviation for a roughly-normal sample."""
    if m is None:
        m = median(values)
    return 1.4826 * median([abs(v - m) for v in values])


@dataclass
class Baseline:
    median: float
    mad: float   # already scaled by 1.4826
    n: int       # samples the baseline was computed from


def compute_baseline(samples: list[float]) -> Baseline:
    m = median(samples)
    return Baseline(median=m, mad=mad(samples, m), n=len(samples))


@dataclass
class AnomalyResult:
    is_anomalous: bool
    z: float | None   # None when mad == 0 and the percentage fallback fired instead
    pct: float        # |value - median| / median * 100, for the email copy


def evaluate(value: float, baseline: Baseline) -> AnomalyResult:
    """Both conditions must hold:  |z| > 3  and  |value - m| > max(0.20*m, 1.0)
    Degenerate fallback when mad == 0 (a perfectly regular category):
    |value - m| > max(0.50*m, 1.0)."""
    m = baseline.median
    diff = abs(value - m)
    pct = (diff / m * 100.0) if m > 0 else (0.0 if diff == 0 else float("inf"))
    if baseline.mad == 0:
        floor = max(0.50 * m, 1.0)
        return AnomalyResult(is_anomalous=diff > floor, z=None, pct=pct)
    z = (value - m) / baseline.mad
    floor = max(0.20 * m, 1.0)
    return AnomalyResult(is_anomalous=(abs(z) > 3 and diff > floor), z=z, pct=pct)


def trailing_same_weekday_dates(target: date, n: int = 8) -> list[date]:
    """The n dates before `target` sharing its weekday, oldest first — e.g. the
    trailing 8 Tuesdays before a Tuesday target. Plain calendar-week
    arithmetic (timedelta weeks), so it is unaffected by DST boundaries."""
    return [target - timedelta(weeks=i) for i in range(n, 0, -1)]
