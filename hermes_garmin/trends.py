"""Deterministic baselines and deviation flags over daily metric series.

For each metric: the latest value is compared with a baseline made of the earlier days in the
window (mean, sample standard deviation, z-score), and the 7-day average with the 28-day average.
A flag needs both a relative change of at least ``flag_pct`` and, for the latest value, a z-score
of at least ``flag_z``, so ordinary day-to-day noise is not reported. The z-score uses a standard
deviation of at least 2% of the baseline mean, so a very regular baseline cannot hide a real change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, stdev
from typing import Iterable, Optional

MIN_BASELINE_DAYS = 5
MIN_SD_FRACTION = 0.02


@dataclass(frozen=True)
class Metric:
    label: str
    unit: str
    better: str  # "higher", "lower" or "" when neither direction is better
    digits: int = 0


METRICS: dict[str, Metric] = {
    "hrv_ms": Metric("overnight HRV", "ms", "higher"),
    "resting_hr_bpm": Metric("resting heart rate", "bpm", "lower"),
    "sleep_hours": Metric("sleep", "h", "higher", 1),
    "sleep_score": Metric("sleep score", "", "higher"),
    "avg_stress": Metric("average stress", "", "lower"),
    "body_battery_high": Metric("Body Battery peak", "", "higher"),
    "steps": Metric("steps", "", ""),
    "intensity_minutes": Metric("intensity minutes", "min", ""),
}


def _fmt(value: float, metric: Metric) -> str:
    number = f"{value:.{metric.digits}f}" if metric.digits else f"{value:.0f}"
    return f"{number} {metric.unit}".strip()


def _pct_change(value: float, reference: float) -> Optional[float]:
    return None if not reference else round(100 * (value - reference) / abs(reference), 1)


def _verdict(change: float, metric: Metric) -> str:
    if not metric.better:
        return "neutral"
    good = change > 0 if metric.better == "higher" else change < 0
    return "favourable" if good else "unfavourable"


def analyze_metric(name: str, points: Iterable[tuple[date, float]], as_of: date, *,
                   flag_pct: float = 10.0, flag_z: float = 1.0) -> tuple[dict, list[dict]]:
    """Stats for one metric plus its flags. ``points`` are ``(day, value)``; later duplicates win."""
    metric = METRICS[name]
    by_day = {day: float(value) for day, value in points if day <= as_of and value is not None}
    if not by_day:
        return {"days_with_data": 0}, []
    days = sorted(by_day)
    values = [by_day[d] for d in days]
    latest_day, latest = days[-1], values[-1]
    baseline = values[:-1]
    last7 = [by_day[d] for d in days if d > as_of - timedelta(days=7)]
    last28 = [by_day[d] for d in days if d > as_of - timedelta(days=28)]
    stats: dict = {
        "days_with_data": len(values),
        "latest": round(latest, metric.digits) if metric.digits else round(latest),
        "latest_date": latest_day.isoformat(),
        "mean": round(mean(values), 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
    }
    if last7:
        stats["avg_7d"] = round(mean(last7), 1)
    if last28:
        stats["avg_28d"] = round(mean(last28), 1)
    flags: list[dict] = []

    if len(baseline) >= MIN_BASELINE_DAYS:
        base_mean = mean(baseline)
        spread = stdev(baseline)
        stats["baseline_mean"] = round(base_mean, 1)
        stats["baseline_sd"] = round(spread, 1)
        change = _pct_change(latest, base_mean)
        stats["latest_vs_baseline_pct"] = change
        floor = max(spread, abs(base_mean) * MIN_SD_FRACTION)
        z = round((latest - base_mean) / floor, 2) if floor > 0 else None
        stats["z_latest"] = z
        if change is not None and z is not None and abs(change) >= flag_pct and abs(z) >= flag_z:
            flags.append({
                "metric": name,
                "kind": "latest_vs_baseline",
                "verdict": _verdict(change, metric),
                "text": (f"{metric.label} {abs(change):.0f}% {'above' if change > 0 else 'below'} baseline "
                         f"({_fmt(latest, metric)} on {latest_day} vs {_fmt(base_mean, metric)}, z={z:+.1f})"),
            })
    else:
        stats["note"] = f"baseline needs at least {MIN_BASELINE_DAYS + 1} days with data"

    if len(last7) >= 3 and len(last28) >= 14:
        sustained = _pct_change(stats["avg_7d"], stats["avg_28d"])
        stats["avg_7d_vs_28d_pct"] = sustained
        if sustained is not None and abs(sustained) >= flag_pct:
            flags.append({
                "metric": name,
                "kind": "7d_vs_28d",
                "verdict": _verdict(sustained, metric),
                "text": (f"7-day {metric.label} average {abs(sustained):.0f}% "
                         f"{'above' if sustained > 0 else 'below'} the 28-day average "
                         f"({_fmt(stats['avg_7d'], metric)} vs {_fmt(stats['avg_28d'], metric)})"),
            })
    if latest_day < as_of - timedelta(days=1):
        stats["stale"] = f"latest value is from {latest_day}"
    return stats, flags


def analyze(series: dict[str, Iterable[tuple[date, float]]], as_of: date, *, window_days: int,
            flag_pct: float = 10.0, flag_z: float = 1.0) -> dict:
    metrics: dict[str, dict] = {}
    flags: list[dict] = []
    for name, points in series.items():
        stats, metric_flags = analyze_metric(name, points, as_of, flag_pct=flag_pct, flag_z=flag_z)
        metrics[name] = stats
        flags.extend(metric_flags)
    order = {"unfavourable": 0, "neutral": 1, "favourable": 2}
    flags.sort(key=lambda f: order[f["verdict"]])
    return {
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "method": (f"baseline = earlier days in the window; flagged when the change is >= {flag_pct:g}% "
                   f"and |z| >= {flag_z:g} (latest), or the 7-day vs 28-day change is >= {flag_pct:g}%"),
        "flags": flags,
        "metrics": metrics,
    }
