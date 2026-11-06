"""Heart-rate zones, training intensity distribution and fitness age. Pure functions over Garmin DTOs.

Time in zone comes from the activity list (``SummarizedActivityDTO.hrTimeInZone_1..5``, seconds);
the activity detail endpoint does not carry it. Zone bounds come from ``/biometric-service/heartRateZones/v2``
and are the user's current settings: Garmin computed each activity's times with the zones in effect
when it was recorded, so bounds edited since then may not match older activities exactly.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Optional

from . import summaries

ZONES = (1, 2, 3, 4, 5)
BASELINE_DAYS = 28

# Flag thresholds, all documented in ``method``.
RATIO_HIGH = 1.5  # zone 4-5 time per week vs the previous 4 weeks
RATIO_LOW = 0.5
TOTAL_RATIO_HIGH = 1.3  # all zoned time per week vs the previous 4 weeks
TOTAL_RATIO_LOW = 0.7
MIN_BASE_HARD_MIN = 10.0  # weekly zone 4-5 minutes needed before a ratio means anything
MIN_BASE_TOTAL_MIN = 60.0
NEW_HARD_MIN = 20.0  # zone 4-5 minutes per week that count as "new" hard work over a near-zero baseline
MIN_SHARE_TOTAL_MIN = 30.0  # zoned minutes in the window needed before shares are flagged
EASY_SHARE = 90.0
HARD_SHARE = 25.0
MIDDLE_SHARE = 40.0

_SPORTS = (("RUNNING", ("run",)), ("CYCLING", ("cycl", "bik", "ride")), ("SWIMMING", ("swim",)),
           ("ROWING", ("row",)))


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _r(value: float, digits: int = 1) -> float:
    return round(value, digits)


# -- zone definitions ---------------------------------------------------------------------------

def zone_sets(dtos: Iterable[Any]) -> list[dict]:
    """``HeartRateZonesDTO`` items with five ascending floors, as ``{sport, method, zones, ...}``.
    Zone N runs from its floor to one below the next floor; zone 5 ends at the max heart rate."""
    out = []
    for dto in dtos or []:
        if not isinstance(dto, Mapping):
            continue
        floors = [dto.get(f"zone{i}Floor") for i in ZONES]
        if not all(isinstance(f, (int, float)) and f > 0 for f in floors):
            continue
        if any(a >= b for a, b in zip(floors, floors[1:])):
            continue
        top = _num(dto.get("maxHeartRateUsed"))
        zones = []
        for i, low in enumerate(floors):
            high = floors[i + 1] - 1 if i < 4 else (top if top and top > low else None)
            zones.append({"zone": i + 1, "low_bpm": int(low), "high_bpm": int(high) if high else None})
        out.append(summaries._clean({
            "sport": str(dto.get("sport") or "DEFAULT").upper(),
            "method": dto.get("trainingMethod"),
            "max_hr_bpm": dto.get("maxHeartRateUsed"),
            "lactate_threshold_bpm": dto.get("lactateThresholdHeartRateUsed"),
            "resting_hr_bpm": dto.get("restingHeartRateUsed"),
            "zones": zones,
        }))
    return out


def sport_for(type_key: Any) -> str:
    key = str(type_key or "").lower()
    for sport, hints in _SPORTS:
        if any(h in key for h in hints):
            return sport
    return "DEFAULT"


def zones_for(sets: list[dict], type_key: Any = "") -> Optional[dict]:
    """The set for the activity's sport, else ``DEFAULT``, else the first one."""
    if not sets:
        return None
    by_sport = {s["sport"]: s for s in sets}
    return by_sport.get(sport_for(type_key)) or by_sport.get("DEFAULT") or sets[0]


def bpm_range(zone: Mapping[str, Any]) -> str:
    high = zone.get("high_bpm")
    return f"{zone['low_bpm']}-{high}" if high else f"{zone['low_bpm']}+"


def zone_seconds(dto: Mapping[str, Any]) -> Optional[dict[int, float]]:
    """Seconds per zone from a ``SummarizedActivityDTO``; None when the activity has no HR zones."""
    values = {i: _num(dto.get(f"hrTimeInZone_{i}")) for i in ZONES}
    if all(v is None for v in values.values()):
        return None
    return {i: max(0.0, v or 0.0) for i, v in values.items()}


# -- one activity -------------------------------------------------------------------------------

def activity_zones(list_dto: Mapping[str, Any], zone_set: Optional[dict]) -> Optional[dict]:
    """Time and share per zone for one activity, with bpm bounds when the zone set is known.

    Shares are of the time Garmin placed in zones 1-5; ``outside_zones_min`` is the rest of the
    activity's duration (below zone 1, or no heart-rate reading)."""
    seconds = zone_seconds(list_dto)
    if seconds is None:
        return None
    total = sum(seconds.values())
    bounds = {z["zone"]: z for z in (zone_set or {}).get("zones", [])}
    rows = []
    for i in ZONES:
        row = {"zone": i, "minutes": _r(seconds[i] / 60),
               "pct": int(round(100 * seconds[i] / total)) if total else 0}
        if i in bounds:
            row["bpm"] = bpm_range(bounds[i])
        rows.append(row)
    out: dict = {"zones": rows, "in_zones_min": _r(total / 60)}
    if total:
        out["easy_pct_z1_z2"] = int(round(100 * (seconds[1] + seconds[2]) / total))
        out["hard_pct_z4_z5"] = int(round(100 * (seconds[4] + seconds[5]) / total))
    duration = _num(list_dto.get("duration"))
    if duration and duration - total >= 60:
        out["outside_zones_min"] = _r((duration - total) / 60)
    if zone_set:
        out["zone_basis"] = summaries._clean({
            "sport": zone_set.get("sport"), "method": zone_set.get("method"),
            "note": "bounds are your current Garmin zone settings"})
    return out


# -- training load over a window ----------------------------------------------------------------

def _activity_day(dto: Mapping[str, Any]) -> Optional[date]:
    return summaries.parse_iso_day(dto.get("startTimeLocal"))


def _minutes(seconds: dict[int, float]) -> dict[str, float]:
    return {f"z{i}": _r(seconds[i] / 60) for i in ZONES}


def _sum_zones(rows: list[dict[int, float]]) -> dict[int, float]:
    return {i: sum(r[i] for r in rows) for i in ZONES}


def _ratio(value: float, reference: float) -> Optional[float]:
    return round(value / reference, 2) if reference > 0 else None


def _fmt_min(minutes: float) -> str:
    minutes = int(round(minutes))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


def _intensity(dailies: Iterable[Any], start: date, end: date) -> dict:
    moderate = vigorous = 0
    days = 0
    goal = None
    for row in dailies or []:
        if not isinstance(row, Mapping):
            continue
        day = summaries.parse_iso_day(row.get("calendarDate"))
        im = row.get("intensityMinutes") if isinstance(row.get("intensityMinutes"), Mapping) else None
        if day is None or im is None or not start <= day <= end:
            continue
        mod, vig = _num(im.get("moderate")), _num(im.get("vigorous"))
        if mod is None and vig is None:
            continue
        days += 1
        moderate += int(mod or 0)
        vigorous += int(vig or 0)
        if _num(im.get("goal")):
            goal = int(im["goal"])
    return {"days": days, "moderate": moderate, "vigorous": vigorous, "total": moderate + 2 * vigorous,
            "goal": goal}


def analyze_load(activities: Iterable[Any], dailies: Iterable[Any], zone_dtos: Iterable[Any], as_of: date,
                 days: int = 7, *, baseline_days: int = BASELINE_DAYS, ends_today: bool = True) -> dict:
    """Time in heart-rate zones over the last ``days`` days against the weekly average of the
    ``baseline_days`` before them, plus intensity minutes and deterministic flags."""
    start = as_of - timedelta(days=days - 1)
    base_start, base_end = start - timedelta(days=baseline_days), start - timedelta(days=1)
    base_weeks = baseline_days / 7
    scale = 7 / days  # window totals as a weekly rate

    sets = zone_sets(zone_dtos)
    default = zones_for(sets)
    window_rows: list[dict[int, float]] = []
    base_rows: list[dict[int, float]] = []
    window_count = base_count = window_without_hr = 0
    by_type: dict[str, dict] = {}
    for dto in activities or []:
        if not isinstance(dto, Mapping):
            continue
        day = _activity_day(dto)
        if day is None:
            continue
        seconds = zone_seconds(dto)
        if start <= day <= as_of:
            window_count += 1
            if seconds is None:
                window_without_hr += 1
                continue
            window_rows.append(seconds)
            label = summaries.activity_label(summaries._get(dto, "activityType", "typeKey"))
            entry = by_type.setdefault(label, {"type_label": label, "activities": 0, "minutes": 0.0,
                                               "z4_z5_min": 0.0})
            entry["activities"] += 1
            entry["minutes"] += sum(seconds.values()) / 60
            entry["z4_z5_min"] += (seconds[4] + seconds[5]) / 60
        elif base_start <= day <= base_end:
            base_count += 1
            if seconds is not None:
                base_rows.append(seconds)

    window = _sum_zones(window_rows)
    base = _sum_zones(base_rows)
    total_min = sum(window.values()) / 60
    hard_min = (window[4] + window[5]) / 60
    base_week = {i: base[i] / base_weeks for i in ZONES}
    base_total_wk = sum(base_week.values()) / 60
    base_hard_wk = (base_week[4] + base_week[5]) / 60
    total_wk, hard_wk = total_min * scale, hard_min * scale

    def share(*zones: int) -> Optional[int]:
        whole = sum(window.values())
        return int(round(100 * sum(window[z] for z in zones) / whole)) if whole else None

    result: dict = {
        "range": f"{start}..{as_of}",
        "days": days,
        "baseline_range": f"{base_start}..{base_end}",
        "window": summaries._clean({
            "activities": window_count,
            "with_hr_zones": len(window_rows),
            "zone_minutes": _minutes(window) if window_rows else None,
            "in_zones": _fmt_min(total_min) if window_rows else None,
            "easy_pct_z1_z2": share(1, 2),
            "middle_pct_z3": share(3),
            "hard_pct_z4_z5": share(4, 5),
            "z4_z5_min": _r(hard_min) if window_rows else None,
        }),
        "baseline_weekly_avg": summaries._clean({
            "activities": _r(base_count / base_weeks),
            "zone_minutes": {f"z{i}": _r(base_week[i] / 60) for i in ZONES} if base_rows else None,
            "in_zones_min": _r(base_total_wk) if base_rows else None,
            "z4_z5_min": _r(base_hard_wk) if base_rows else None,
        }),
    }
    if days != 7 and window_rows:
        result["window_per_week"] = {"in_zones_min": _r(total_wk), "z4_z5_min": _r(hard_wk)}
    comparison = summaries._clean({
        "in_zones_ratio": _ratio(total_wk, base_total_wk) if base_total_wk >= MIN_BASE_TOTAL_MIN else None,
        "z4_z5_ratio": _ratio(hard_wk, base_hard_wk) if base_hard_wk >= MIN_BASE_HARD_MIN else None,
    })
    if comparison:
        result["vs_baseline"] = comparison
    if by_type:
        result["by_type"] = sorted(
            ({**e, "minutes": _r(e["minutes"]), "z4_z5_min": _r(e["z4_z5_min"])} for e in by_type.values()),
            key=lambda e: -e["minutes"])
    if default:
        result["zones"] = summaries._clean({
            "sport": default.get("sport"), "method": default.get("method"),
            "bpm": {f"z{z['zone']}": bpm_range(z) for z in default["zones"]},
            "note": "current settings; each activity's times use the zones in effect when it was recorded"
                    + (" and its sport's zones" if len(sets) > 1 else ""),
        })

    window_im = _intensity(dailies, start, as_of)
    base_im = _intensity(dailies, base_start, base_end)
    if window_im["days"]:
        im: dict = {"moderate": window_im["moderate"], "vigorous": window_im["vigorous"],
                    "total": window_im["total"], "counting": "moderate + 2 x vigorous, as Garmin does"}
        if days != 7:
            im["per_week"] = int(round(window_im["total"] * scale))
        if window_im["goal"]:
            im["weekly_goal"] = window_im["goal"]
        if base_im["days"] >= 14:
            im["baseline_weekly_avg"] = int(round(base_im["total"] / (base_im["days"] / 7)))
        im["note"] = ("a rolling window; Garmin's own weekly goal runs Monday-Sunday"
                      + ("; today counts up to the watch's last sync" if ends_today else ""))
        result["intensity_minutes"] = im

    flags = _flags(result, total_min, hard_wk, base_hard_wk, total_wk, base_total_wk, days, base_rows)
    result["flags"] = flags
    if not window_rows:
        result["note"] = (f"No activity with heart-rate zones between {start} and {as_of}."
                          + (f" {window_without_hr} activity(ies) had no heart-rate data." if window_without_hr else ""))
    elif window_without_hr:
        result["note"] = f"{window_without_hr} activity(ies) in the window had no heart-rate data and are not counted."
    result["method"] = (
        f"Sums Garmin's time in heart-rate zones of every activity started in the window; the baseline is the "
        f"weekly average of the {baseline_days} days before it. Flags: zone 4-5 time >= {RATIO_HIGH:g}x or <= "
        f"{RATIO_LOW:g}x the baseline (baseline >= {MIN_BASE_HARD_MIN:g} min/week); all zoned time >= "
        f"{TOTAL_RATIO_HIGH:g}x or <= {TOTAL_RATIO_LOW:g}x (baseline >= {MIN_BASE_TOTAL_MIN:g} min/week); with "
        f">= {MIN_SHARE_TOTAL_MIN:g} zoned minutes, zones 1-2 >= {EASY_SHARE:g}%, zone 3 >= {MIDDLE_SHARE:g}% or "
        f"zones 4-5 >= {HARD_SHARE:g}% of the time. Periods other than 7 days are scaled to a weekly rate.")
    return result


def _flags(result: dict, total_min: float, hard_wk: float, base_hard_wk: float, total_wk: float,
           base_total_wk: float, days: int, base_rows: list) -> list[dict]:
    flags: list[dict] = []
    window = result["window"]
    period = "this week" if days == 7 else f"over {days} days (per week)"

    if base_hard_wk >= MIN_BASE_HARD_MIN:
        ratio = hard_wk / base_hard_wk
        if ratio >= RATIO_HIGH or ratio <= RATIO_LOW:
            flags.append({
                "kind": "z4_z5_vs_baseline", "direction": "up" if ratio > 1 else "down", "caution": ratio >= RATIO_HIGH,
                "text": (f"Zone 4-5 time {ratio:.1f}x your 4-week weekly average "
                         f"({_fmt_min(hard_wk)} {period} vs {_fmt_min(base_hard_wk)}/week)")})
    elif base_rows and hard_wk >= NEW_HARD_MIN:
        flags.append({
            "kind": "z4_z5_vs_baseline", "direction": "up", "caution": True,
            "text": (f"{_fmt_min(hard_wk)} in zones 4-5 {period}, up from almost none "
                     f"({_fmt_min(base_hard_wk)}/week over the previous 4 weeks)")})

    if base_total_wk >= MIN_BASE_TOTAL_MIN:
        ratio = total_wk / base_total_wk
        if ratio >= TOTAL_RATIO_HIGH or ratio <= TOTAL_RATIO_LOW:
            flags.append({
                "kind": "volume_vs_baseline", "direction": "up" if ratio > 1 else "down",
                "caution": ratio >= RATIO_HIGH,
                "text": (f"Training time in heart-rate zones {ratio:.1f}x your 4-week weekly average "
                         f"({_fmt_min(total_wk)} {period} vs {_fmt_min(base_total_wk)}/week)")})

    if total_min >= MIN_SHARE_TOTAL_MIN:
        easy, middle, hard = (window.get(k) for k in ("easy_pct_z1_z2", "middle_pct_z3", "hard_pct_z4_z5"))
        if easy is not None and easy >= EASY_SHARE:
            flags.append({"kind": "distribution", "direction": "easy", "caution": False,
                          "text": f"{easy}% of time in zones 1-2: almost all easy"})
        if middle is not None and middle >= MIDDLE_SHARE:
            flags.append({"kind": "distribution", "direction": "middle", "caution": False,
                          "text": f"{middle}% of time in zone 3, the moderate middle"})
        if hard is not None and hard >= HARD_SHARE:
            flags.append({"kind": "distribution", "direction": "hard", "caution": False,
                          "text": f"{hard}% of time in zones 4-5"})

    im = result.get("intensity_minutes") or {}
    goal, total = im.get("weekly_goal"), im.get("total")
    if days == 7 and goal and total is not None:
        pct = int(round(100 * total / goal))
        flags.append({"kind": "intensity_minutes_goal", "direction": "up" if pct >= 100 else "down",
                      "caution": False,
                      "text": f"{total} of {goal} weekly intensity minutes ({pct}%) in the last 7 days"})
    return flags


# -- fitness age --------------------------------------------------------------------------------

# Garmin component key -> (output name, label, unit, which way is better)
FITNESS_AGE_COMPONENTS = {
    "vigorousDaysAvg": ("vigorous_days_per_week", "Days per week with vigorous activity", "days/week", "higher"),
    "vigorousMinutesAvg": ("vigorous_minutes_per_week", "Vigorous intensity minutes per week", "min/week", "higher"),
    "rhr": ("resting_hr", "Resting heart rate", "bpm", "lower"),
    "bmi": ("bmi", "BMI", "", "lower"),
    "bodyFat": ("body_fat", "Body fat", "%", "lower"),
    "bodyFatPercentage": ("body_fat", "Body fat", "%", "lower"),
}

INVALID_REASONS = {
    "USER_EMPTY_AGE": "Garmin has no birth date for this account (set it in the Garmin Connect profile).",
    "USER_UNDER_AGE": "Garmin does not compute fitness age below its minimum age.",
    "USER_OVER_AGE": "Garmin does not compute fitness age above its maximum age.",
    "USER_PREGNANT": "Garmin pauses fitness age during pregnancy tracking.",
}


def _fmt_value(value: Optional[float], unit: str) -> str:
    if value is None:
        return "?"
    text = f"{value:.1f}".rstrip("0").rstrip(".") if value != int(value) else f"{int(value)}"
    return f"{text} {unit}".strip()


def fitness_age(dto: Optional[Mapping[str, Any]]) -> dict:
    """``FitnessAgeDTO`` as fitness age vs actual age, each component against Garmin's target, and
    the components to work on, most effective first (Garmin's ``priority`` 1 = first)."""
    if not dto:
        return {"note": "Garmin has no fitness age for this account yet (it needs a birth date and some "
                        "activity with heart rate)."}
    reason = dto.get("invalidReason")
    fitness, age = _num(dto.get("fitnessAge")), _num(dto.get("chronologicalAge"))
    if reason or fitness is None:
        return summaries._clean({"invalid_reason": reason,
                                 "note": INVALID_REASONS.get(str(reason), "Garmin returned no fitness age.")})
    previous = _num(dto.get("previousFitnessAge"))
    achievable = _num(dto.get("achievableFitnessAge"))
    components = []
    raw = dto.get("components") if isinstance(dto.get("components"), Mapping) else {}
    for key, comp in raw.items():
        if not isinstance(comp, Mapping):
            continue
        name, label, unit, better = FITNESS_AGE_COMPONENTS.get(
            key, (key, key[0].upper() + key[1:], "", ""))
        value, target = _num(comp.get("value")), _num(comp.get("targetValue"))
        on_target = None
        if value is not None and target is not None and better:
            on_target = value >= target if better == "higher" else value <= target
        potential = _num(comp.get("potentialAge"))
        components.append(summaries._clean({
            "name": name,
            "label": label,
            "value": _r(value) if value is not None else None,
            "unit": unit,
            "target": _r(target) if target is not None else None,
            "better": better,
            "on_target": on_target,
            # Garmin's potential age when this component reaches its target. Live data had values above
            # the current fitness age for components still below target, so only lower ones are kept.
            "fitness_age_at_target": _r(potential) if potential is not None and potential < fitness else None,
            "priority": comp.get("priority"),
            "stale": True if comp.get("stale") else None,
            "last_measured": comp.get("lastMeasurementDate"),
            "weeks_of_data": comp.get("numOfWeeksForIm"),
        }))
    components.sort(key=lambda c: (c.get("priority") is None, c.get("priority") or 0, c["name"]))
    improve = []
    for c in components:
        if c.get("on_target") is False:
            text = f"{c['label']}: {_fmt_value(c.get('value'), c.get('unit', ''))} -> target {_fmt_value(c.get('target'), c.get('unit', ''))}"
            if c.get("fitness_age_at_target") is not None:
                text += f" (fitness age about {c['fitness_age_at_target']:.0f} at target)"
            improve.append(text)
    out = {
        "fitness_age": _r(fitness),
        "chronological_age": int(age) if age is not None else None,
        "years_vs_age": _r(fitness - age) if age is not None else None,
        "reading": (f"{abs(fitness - age):.1f} years {'younger' if fitness < age else 'older'} than your age"
                    if age is not None and abs(fitness - age) >= 0.1 else
                    "the same as your age" if age is not None else None),
        "previous_fitness_age": _r(previous) if previous is not None else None,
        "change_since_previous": _r(fitness - previous) if previous is not None and previous != fitness else None,
        "achievable_fitness_age": _r(achievable) if achievable is not None else None,
        "last_updated": str(dto.get("lastUpdated") or "")[:10] or None,
        "components": components,
        "to_improve": improve,
    }
    stale = [c["label"] for c in components if c.get("stale")]
    if stale:
        out["stale_note"] = f"Garmin marks these inputs as out of date: {', '.join(stale)}."
    return summaries._clean(out)
