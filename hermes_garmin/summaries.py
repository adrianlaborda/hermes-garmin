"""Raw Garmin DTOs to compact, model-friendly dicts.

Units are in the key names (``_bpm``, ``_km``, ``_ms``...); durations are ``h:mm`` strings and
times are local ``YYYY-MM-DDTHH:MM``. Location, owner and social fields are dropped. Every field
is optional: the live API omits many that the spec lists.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Iterable, Mapping, Optional

# Activity types where pace reads better than speed.
_PACE_TYPES = ("running", "walking", "hiking", "trail_running", "treadmill_running", "track_running")

# Type keys whose plain humanised form ("Hiit", "E bike fitness") reads wrong.
_TYPE_LABELS = {
    "hiit": "HIIT",
    "e_bike_fitness": "E-bike",
    "e_bike_mountain": "E-mountain bike",
    "e_enduro_mtb": "E-enduro MTB",
    "lap_swimming": "Pool swim",
    "open_water_swimming": "Open water swim",
    "mountain_biking": "Mountain biking",
    "virtual_ride": "Virtual ride",
    "virtual_run": "Virtual run",
    "bmx": "BMX",
    "sup": "Stand up paddling",
    "uncategorized": "Activity",
    "other": "Activity",
}


def activity_label(type_key: Any) -> str:
    """A Garmin activity type key as a label: ``strength_training`` -> ``Strength training``."""
    key = str(type_key or "").strip().lower()
    if not key:
        return "Activity"
    return _TYPE_LABELS.get(key) or key.replace("_", " ").capitalize()


# -- formatting ---------------------------------------------------------------------------------

def hmm(seconds: Any) -> Optional[str]:
    """Seconds as ``h:mm`` (``25500`` -> ``"7:05"``)."""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}:{minutes % 60:02d}"


def hms(seconds: Any) -> Optional[str]:
    """Seconds as ``h:mm:ss``, for activity durations."""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    total = int(round(seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def local_time(value: Any, utc_to: Optional[tzinfo] = None) -> Optional[str]:
    """A Garmin timestamp as local ``YYYY-MM-DDTHH:MM``.

    Garmin sends epoch milliseconds or ISO strings without offset. ``*Local`` fields already hold
    the wall-clock time (epoch values shifted by the user's offset), so they are read as UTC and
    printed as is. ``*GMT`` fields are real UTC: pass ``utc_to`` to convert them to the user's zone."""
    moment: Optional[datetime] = None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        moment = datetime.fromtimestamp(value / 1000, timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace(" ", "T")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            try:
                moment = datetime.fromisoformat(text[:19])
            except ValueError:
                return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    if moment is None:
        return None
    if utc_to is not None:
        moment = moment.astimezone(utc_to)
    return moment.strftime("%Y-%m-%dT%H:%M")


def _num(value: Any, digits: int = 0) -> Optional[float | int]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return int(round(value)) if digits == 0 else round(float(value), digits)


def _pct(part: Any, whole: Any) -> Optional[int]:
    if isinstance(part, (int, float)) and isinstance(whole, (int, float)) and whole > 0:
        return int(round(100 * part / whole))
    return None


def _clean(data: Mapping[str, Any]) -> dict:
    """Drop None, empty strings and empty containers (recursively for nested dicts)."""
    out = {}
    for key, value in data.items():
        if isinstance(value, Mapping):
            value = _clean(value)
        if value is None or value == "" or value == [] or value == {}:
            continue
        out[key] = value
    return out


def _get(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _mass_kg(value: Any) -> Optional[float]:
    """Garmin's weight fields carry no unit in the spec; live values are reported in grams.
    Anything above 500 is treated as grams, so a kilogram value also passes through."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return round(value / 1000 if value > 500 else value, 1)


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace(" ", "T")[:19])
    except ValueError:
        return None


def utc_offset(dto: Mapping[str, Any]) -> Optional[timedelta]:
    """The user's UTC offset for that day, from a pair of Garmin ``*Gmt``/``*Local`` fields."""
    for gmt_key, local_key in (("wellnessStartTimeGmt", "wellnessStartTimeLocal"),
                               ("wellnessEndTimeGmt", "wellnessEndTimeLocal"),
                               ("startTimestampGMT", "startTimestampLocal")):
        gmt, local = _parse(dto.get(gmt_key)), _parse(dto.get(local_key))
        if gmt and local:
            return local - gmt
    return None


def last_sync_local(dto: Mapping[str, Any], utc_to: Optional[tzinfo] = None) -> Optional[str]:
    """When the watch last synced, as the user's local ``YYYY-MM-DDTHH:MM``.

    ``UserDailySummaryDto.lastSyncTimestampGMT`` is UTC; the day's own GMT/local pair gives the
    offset, so this is right even when the server runs in another zone. Falls back to the end of
    the day's wellness data, which matches the last sync."""
    synced = _parse(dto.get("lastSyncTimestampGMT"))
    offset = utc_offset(dto)
    if synced is not None:
        if offset is not None:
            return (synced + offset).strftime("%Y-%m-%dT%H:%M")
        return local_time(dto.get("lastSyncTimestampGMT"), utc_to)
    return local_time(dto.get("wellnessEndTimeLocal"))


def wellness_end_local(dto: Mapping[str, Any]) -> Optional[str]:
    """Where a ``UserDailySummaryV2DTO``'s wellness data ends, local time: today, the last sync."""
    return local_time(_get(dto, "wellnessChronology", "endTimestampLocal"))


# -- wellness -----------------------------------------------------------------------------------

def sleep(dto: Mapping[str, Any]) -> dict:
    """``DailySleepDTO``. Its ``calendarDate`` is the day you woke up."""
    total = dto.get("sleepTimeSeconds")
    scores = dto.get("sleepScores") if isinstance(dto.get("sleepScores"), Mapping) else {}
    overall = scores.get("overall") if isinstance(scores.get("overall"), Mapping) else {}
    stages = {}
    for name, key in (("deep", "deepSleepSeconds"), ("light", "lightSleepSeconds"),
                      ("rem", "remSleepSeconds"), ("awake", "awakeSleepSeconds")):
        if isinstance(dto.get(key), (int, float)):
            stages[name] = {"hmm": hmm(dto[key]), "pct": _pct(dto[key], total) if name != "awake" else None}
    return _clean({
        "date": dto.get("calendarDate"),
        "total_sleep_hmm": hmm(total),
        "stages": stages,
        "score": _num(overall.get("value")),
        "score_quality": overall.get("qualifierKey"),
        "bedtime_local": local_time(dto.get("sleepStartTimestampLocal")),
        "wake_local": local_time(dto.get("sleepEndTimestampLocal")),
        "avg_hr_bpm": _num(dto.get("avgHeartRate")),
        "avg_respiration_brpm": _num(dto.get("averageRespirationValue"), 1),
        "avg_spo2_pct": _num(dto.get("averageSpO2Value")),
        "lowest_spo2_pct": _num(dto.get("lowestSpO2Value")),
        "avg_sleep_stress": _num(dto.get("avgSleepStress")),
        "awake_count": dto.get("awakeCount"),
        "naps_hmm": hmm(dto.get("napTimeSeconds")) if dto.get("napTimeSeconds") else None,
        "feedback": dto.get("sleepScoreFeedback"),
        "insight": dto.get("sleepScoreInsight") if dto.get("sleepScoreInsight") not in ("NONE", None) else None,
        "breathing_disruption": dto.get("breathingDisruptionSeverity"),
    })


# Sleep-score components: Garmin key -> (output name, how the actual value is read from the DTO)
_SCORE_PARTS = (
    ("totalDuration", "duration", "sleepTimeSeconds"),
    ("deepPercentage", "deep_pct", None),
    ("lightPercentage", "light_pct", None),
    ("remPercentage", "rem_pct", None),
    ("awakeCount", "awakenings", "awakeCount"),
    ("restlessness", "restlessness", None),
    ("stress", "sleep_stress", "avgSleepStress"),
)
_RATING_ORDER = {"EXCELLENT": 0, "GOOD": 1, "FAIR": 2, "POOR": 3}
_NOT_SET = (None, "", "NONE", "NOT_AVAILABLE")

STAGE_TIMELINE_NOTE = (
    "Garmin's Personal API gives stage totals, not a stage timeline, so when each stage happened, time to "
    "deep sleep and the length of each awakening are not available. awakenings is Garmin's count.")


def _score_range(start: Any, end: Any, as_duration: bool) -> Optional[str]:
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    if as_duration:
        return hmm(start) if start == end else f"{hmm(start)}-{hmm(end)}"
    return f"{start:g}" if start == end else f"{start:g}-{end:g}"


def sleep_detail(dto: Mapping[str, Any]) -> dict:
    """One night's ``DailySleepDTO`` beyond the summary: each sleep-score component with its value,
    Garmin's rating and optimal range, the components rated below GOOD, respiration range, sleep need,
    alignment and naps when Garmin sends them."""
    scores = dto.get("sleepScores") if isinstance(dto.get("sleepScores"), Mapping) else {}
    parts = []
    for key, name, source in _SCORE_PARTS:
        item = scores.get(key)
        if not isinstance(item, Mapping):
            continue
        duration = key == "totalDuration"
        value = dto.get(source) if source else item.get("value")
        parts.append(_clean({
            "component": name,
            "value": hmm(value) if duration else _num(value, 1) if isinstance(value, float) else value,
            "unit": "%" if name.endswith("_pct") else None,
            "rating": item.get("qualifierKey"),
            "optimal": _score_range(item.get("optimalStart"), item.get("optimalEnd"), duration),
        }))
    weak = sorted((p for p in parts if _RATING_ORDER.get(str(p.get("rating")), -1) >= 2),
                  key=lambda p: -_RATING_ORDER[p["rating"]])
    need = dto.get("sleepNeed") if isinstance(dto.get("sleepNeed"), Mapping) else {}
    alignment = dto.get("sleepAlignment") if isinstance(dto.get("sleepAlignment"), Mapping) else {}
    naps = [_clean({"start_local": _nap_start(n), "duration_hmm": hmm(n.get("napTimeSec")),
                    "feedback": n.get("napFeedback") if n.get("napFeedback") not in _NOT_SET else None})
            for n in dto.get("dailyNapDTOS") or [] if isinstance(n, Mapping)]
    insight = dto.get("sleepScorePersonalizedInsight")
    return _clean({
        "score_components": parts,
        "below_good": [p["component"] for p in weak],
        "respiration_brpm": _clean({"avg": _num(dto.get("averageRespirationValue"), 1),
                                    "lowest": _num(dto.get("lowestRespirationValue"), 1),
                                    "highest": _num(dto.get("highestRespirationValue"), 1)}),
        "avg_sleep_stress": _num(dto.get("avgSleepStress")),
        "unmeasurable_hmm": hmm(dto.get("unmeasurableSleepSeconds")) if dto.get("unmeasurableSleepSeconds") else None,
        "personalized_insight": insight if insight not in _NOT_SET else None,
        "sleep_need": _clean({
            "baseline_hmm": hmm((need.get("baseline") or 0) * 60) if need.get("baseline") else None,
            "tonight_hmm": hmm((need.get("actual") or 0) * 60) if need.get("actual") else None,
            "feedback": need.get("feedback") if need.get("feedback") not in _NOT_SET else None,
        }),
        "alignment": alignment.get("status") if alignment.get("status") not in _NOT_SET else None,
        "naps": [n for n in naps if n],
        "spo2_events_below_threshold": dto.get("numberOfEventsBelowThreshold"),
        "window": dto.get("sleepWindowConfirmationType"),
        "timeline_note": STAGE_TIMELINE_NOTE,
    })


def _nap_start(nap: Mapping[str, Any]) -> Optional[str]:
    """A nap's local start: its GMT time shifted by ``napStartTimeOffset`` (minutes)."""
    start = _parse(nap.get("napStartTimestampGMT"))
    offset = nap.get("napStartTimeOffset")
    if start is None or not isinstance(offset, (int, float)):
        return None
    return (start + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M")


def hrv_night(chart: Mapping[str, Any], bucket_minutes: int = 0) -> dict:
    """``HrvChartDTO`` readings (every 5 minutes) as ``bucket_minutes`` averages in local time, the first vs
    second half of the night, and the lowest and highest bucket."""
    readings = []
    for item in chart.get("hrvReadings") or []:
        if not isinstance(item, Mapping):
            continue
        moment, value = _parse(item.get("readingTimeLocal")), item.get("hrvValue")
        if moment is not None and isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            readings.append((moment, float(value)))
    readings.sort()
    if not readings:
        return {"note": "Garmin returned no overnight HRV readings for this night."}
    if not bucket_minutes:  # 30-minute buckets keep a normal night under ~20 rows
        bucket_minutes = 30 if readings[-1][0] - readings[0][0] <= timedelta(hours=12) else 60
    buckets: dict[datetime, list[float]] = {}
    for moment, value in readings:
        start = moment.replace(minute=moment.minute - moment.minute % bucket_minutes, second=0, microsecond=0)
        buckets.setdefault(start, []).append(value)
    rows = [(start, sum(v) / len(v)) for start, v in sorted(buckets.items())]
    half = len(readings) // 2
    first = [v for _m, v in readings[:half]] or [readings[0][1]]
    second = [v for _m, v in readings[half:]]

    def span(start: datetime) -> str:
        return f"{start:%H:%M}-{start + timedelta(minutes=bucket_minutes):%H:%M}"
    low = min(rows, key=lambda r: r[1])
    high = max(rows, key=lambda r: r[1])
    first_avg, second_avg = sum(first) / len(first), sum(second) / len(second)
    return _clean({
        "readings": len(readings),
        "window_local": f"{readings[0][0]:%H:%M}-{readings[-1][0]:%H:%M}",
        "bucket_minutes": bucket_minutes,
        "buckets_avg_ms": [[f"{start:%H:%M}", int(round(avg))] for start, avg in rows],
        "first_half_avg_ms": int(round(first_avg)),
        "second_half_avg_ms": int(round(second_avg)),
        "second_vs_first_half_pct": int(round(100 * (second_avg - first_avg) / first_avg)) if first_avg else None,
        "lowest_bucket": {"time": span(low[0]), "avg_ms": int(round(low[1]))},
        "highest_bucket": {"time": span(high[0]), "avg_ms": int(round(high[1]))},
        "min_reading_ms": int(min(v for _m, v in readings)),
        "max_reading_ms": int(max(v for _m, v in readings)),
    })


def sleep_score(dto: Mapping[str, Any]) -> Optional[int]:
    return _num(_get(dto, "sleepScores", "overall", "value"))


def hrv(dto: Mapping[str, Any]) -> dict:
    """``HrvSummaryDTO`` (overnight HRV, RMSSD in milliseconds).

    Garmin's ``status`` rates the 7-day average against the balanced range, not last night, so a
    BALANCED status can sit next to a single night below the range. ``last_night_vs_range`` and
    ``reading`` say which is which."""
    baseline = dto.get("baseline") if isinstance(dto.get("baseline"), Mapping) else {}
    low, high = baseline.get("balancedLow"), baseline.get("balancedUpper")
    night, weekly = dto.get("lastNightAvg"), dto.get("weeklyAvg")
    has_range = isinstance(low, (int, float)) and isinstance(high, (int, float)) and low and high
    position = None
    if has_range and isinstance(night, (int, float)):
        position = "below" if night < low else "above" if night > high else "within"
    return _clean({
        "date": dto.get("calendarDate"),
        "last_night_avg_ms": night,
        "last_night_5min_high_ms": dto.get("lastNight5MinHigh"),
        "weekly_avg_ms": weekly,
        "status": dto.get("status"),
        "status_basis": "7-day average" if dto.get("status") else None,
        "balanced_range_ms": f"{low}-{high}" if has_range else None,
        "last_night_vs_range": position,
        "reading": hrv_reading(night, weekly, dto.get("status"), low, high) if has_range else None,
        "feedback": dto.get("feedbackPhrase"),
    })


def hrv_status_label(status: Any) -> str:
    text = str(status or "").strip()
    return "" if not text or text.upper() in ("NONE", "NO_STATUS") else text.replace("_", " ").capitalize()


def hrv_reading(night: Any, weekly: Any, status: Any, low: Any, high: Any) -> Optional[str]:
    """One sentence that keeps last night and the 7-day status apart."""
    parts = []
    label = hrv_status_label(status)
    if isinstance(weekly, (int, float)):
        parts.append(f"7-day average {weekly} ms" + (f" ({label})" if label else ""))
    if isinstance(night, (int, float)):
        where = ("below" if night < low else "above" if night > high else "within")
        parts.append(f"last night {night} ms, {where} the usual {low}-{high} ms range")
    return "; ".join(parts) or None


def _series(rows: Any, descriptors: Any, key_hint: str, index_key: str, name_key: str) -> list[tuple]:
    """``(timestamp, value)`` pairs from Garmin's column-described arrays."""
    columns = {}
    for item in descriptors or []:
        if isinstance(item, Mapping):
            columns[str(item.get(name_key, "")).lower()] = item.get(index_key)
    ts_index = next((i for name, i in columns.items() if "timestamp" in name), 0)
    value_index = next((i for name, i in columns.items() if key_hint in name), 1)
    out = []
    for row in rows or []:
        if isinstance(row, (list, tuple)) and len(row) > max(ts_index, value_index):
            ts, value = row[ts_index], row[value_index]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append((ts, value))
    return out


def body_battery(dto: Mapping[str, Any], tz: tzinfo, *, events: bool = True) -> dict:
    """``BodyBatteryDailyReportDTO``. Levels are 0-100. The Personal API was not seen to send the
    ``bodyBatteryActivityEvent`` list the spec describes; events are summarised when present."""
    points = _series(dto.get("bodyBatteryValuesArray"), dto.get("bodyBatteryValueDescriptorDTOList"),
                     "level", "bodyBatteryValueDescriptorIndex", "bodyBatteryValueDescriptorKey")
    # The Personal API sends a handful of points per day (6 observed), not the full curve.
    levels = [v for _ts, v in sorted(points, key=lambda p: p[0] if isinstance(p[0], (int, float)) else 0)]
    impacts = []
    for event in (dto.get("bodyBatteryActivityEvent") or [])[:12] if events else ():
        if isinstance(event, Mapping):
            impacts.append(_clean({
                "type": event.get("eventType"),
                "start_local": local_time(event.get("eventStartTimeGmt"), tz),
                "duration_min": _num((event.get("durationInMilliseconds") or 0) / 60000) or None,
                "impact": event.get("bodyBatteryImpact"),
                "feedback": event.get("shortFeedback"),
            }))
    return _clean({
        "date": dto.get("date"),
        "charged": dto.get("charged"),
        "drained": dto.get("drained"),
        "highest": max(levels) if levels else None,
        "lowest": min(levels) if levels else None,
        "latest": levels[-1] if levels else None,
        "feedback": _get(dto, "endOfDayBodyBatteryDynamicFeedbackEvent", "feedbackLongType")
        or _get(dto, "bodyBatteryDynamicFeedbackEvent", "feedbackLongType"),
        "events": impacts,
    })


def stress(dto: Mapping[str, Any]) -> dict:
    """``MultiValueWellnessChartItemDTO``: stress level 0-100, durations in seconds."""
    values = dto.get("values") if isinstance(dto.get("values"), Mapping) else {}
    return _clean({
        "date": dto.get("calendarDate"),
        "avg_stress": _nonneg(values.get("overallStressLevel")),
        "high_hmm": hmm(values.get("highStressDuration")),
        "medium_hmm": hmm(values.get("mediumStressDuration")),
        "low_hmm": hmm(values.get("lowStressDuration")),
        "rest_hmm": hmm(values.get("restStressDuration")),
    })


def steps(dto: Mapping[str, Any]) -> dict:
    """``StepsDailyStatDTO``; distance is in meters."""
    total = dto.get("totalSteps")
    return _clean({
        "date": dto.get("calendarDate"),
        "steps": total,
        "goal": dto.get("stepGoal"),
        "goal_pct": _pct(total, dto.get("stepGoal")),
        "distance_km": _num((dto.get("totalDistance") or 0) / 1000, 2) or None,
    })


def heart_rate(dto: Mapping[str, Any], tz: tzinfo, *, hourly: bool = False) -> dict:
    """``DailyHeartRateDTO``. ``hourly`` adds the mean bpm per local hour."""
    day = dto.get("calendarDate")
    out = {
        "date": day.get("localDate") if isinstance(day, Mapping) else day,
        "resting_bpm": dto.get("restingHeartRate"),
        "min_bpm": dto.get("minHeartRate"),
        "max_bpm": dto.get("maxHeartRate"),
        "resting_7d_avg_bpm": dto.get("lastSevenDaysAvgRestingHeartRate"),
    }
    if hourly:
        buckets: dict[str, list] = {}
        for ts, bpm in _series(dto.get("heartRateValues"), dto.get("heartRateValueDescriptors"),
                               "heartrate", "index", "key"):
            stamp = local_time(ts, tz)
            if stamp:
                buckets.setdefault(stamp[11:13], []).append(bpm)
        out["hourly_avg_bpm"] = {hour: int(round(sum(v) / len(v))) for hour, v in sorted(buckets.items())}
    return _clean(out)


def daily_summary(dto: Mapping[str, Any], utc_to: Optional[tzinfo] = None) -> dict:
    """``UserDailySummaryDto`` (one day of steps, heart rate, stress, Body Battery).

    Values are running totals up to ``last_sync_local``: anything after the watch's last sync
    is not in them yet."""
    return _clean({
        "date": dto.get("calendarDate"),
        "last_sync_local": last_sync_local(dto, utc_to),
        "steps": dto.get("totalSteps"),
        "step_goal": dto.get("dailyStepGoal"),
        "distance_km": _num((dto.get("totalDistanceMeters") or 0) / 1000, 2) or None,
        "active_kcal": _num(dto.get("activeKilocalories")),
        "total_kcal": _num(dto.get("totalKilocalories")),
        "intensity_minutes": _intensity(dto.get("moderateIntensityMinutes"), dto.get("vigorousIntensityMinutes")),
        "intensity_minutes_goal": dto.get("intensityMinutesGoal"),
        "floors": _num(dto.get("floorsAscended")),
        "resting_hr_bpm": dto.get("restingHeartRate"),
        "resting_hr_7d_avg_bpm": dto.get("lastSevenDaysAvgRestingHeartRate"),
        "max_hr_bpm": dto.get("maxHeartRate"),
        "avg_stress": _nonneg(dto.get("averageStressLevel")),
        "max_stress": _nonneg(dto.get("maxStressLevel")),
        "stress_qualifier": dto.get("stressQualifier"),
        "body_battery": {
            "latest": dto.get("bodyBatteryMostRecentValue"),
            "at_wake": dto.get("bodyBatteryAtWakeTime"),
            "highest": dto.get("bodyBatteryHighestValue"),
            "lowest": dto.get("bodyBatteryLowestValue"),
            "charged": dto.get("bodyBatteryChargedValue"),
            "drained": dto.get("bodyBatteryDrainedValue"),
        },
        "avg_spo2_pct": _num(dto.get("averageSpo2")),
        "avg_waking_respiration_brpm": _num(dto.get("avgWakingRespirationValue"), 1),
    })


def daily_summary_v2(dto: Mapping[str, Any]) -> dict:
    """``UserDailySummaryV2DTO`` reduced to the per-day values trends use."""
    return _clean({
        "date": dto.get("calendarDate"),
        "resting_hr_bpm": _get(dto, "heartRate", "restingValue"),
        "min_hr_bpm": _get(dto, "heartRate", "minValue"),
        "max_hr_bpm": _get(dto, "heartRate", "maxValue"),
        "steps": _get(dto, "movement", "steps", "value"),
        "avg_stress": _nonneg(_get(dto, "stress", "avgLevel")),
        "body_battery_high": _get(dto, "bodyBattery", "maxValue"),
        "body_battery_low": _get(dto, "bodyBattery", "minValue"),
        "body_battery_charged": _get(dto, "bodyBattery", "chargedValue"),
        "intensity_minutes": _intensity(_get(dto, "intensityMinutes", "moderate"),
                                        _get(dto, "intensityMinutes", "vigorous")),
    })


def _nonneg(value: Any) -> Any:
    """Garmin uses negative stress levels (-1, -2) for 'not enough data'."""
    return value if isinstance(value, (int, float)) and value >= 0 else None


def _intensity(moderate: Any, vigorous: Any) -> Optional[int]:
    if moderate is None and vigorous is None:
        return None
    return (moderate or 0) + 2 * (vigorous or 0)


def weight(summary: Mapping[str, Any]) -> dict:
    """``DailyWeightSummary`` -> the day's latest measurement."""
    latest = summary.get("latestWeight") if isinstance(summary.get("latestWeight"), Mapping) else {}
    return _clean({
        "date": summary.get("summaryDate") or latest.get("calendarDate"),
        "weight_kg": _mass_kg(latest.get("weight")),
        "bmi": _num(latest.get("bmi"), 1),
        "body_fat_pct": _num(latest.get("bodyFat"), 1),
        "body_water_pct": _num(latest.get("bodyWater"), 1),
        "muscle_mass_kg": _mass_kg(latest.get("muscleMass")),
        "bone_mass_kg": _mass_kg(latest.get("boneMass")),
        "measurements": summary.get("numOfWeightEntries"),
    })


def hydration(dto: Mapping[str, Any]) -> dict:
    """``UserDailyHydrationDTO`` (milliliters)."""
    return _clean({
        "date": dto.get("calendarDate"),
        "intake_ml": _num(dto.get("valueInML")),
        "goal_ml": _num(dto.get("goalInML")),
        "sweat_loss_ml": _num(dto.get("sweatLossInML")),
    })


# -- activities ---------------------------------------------------------------------------------

def _pace_or_speed(type_key: str, speed_mps: Any) -> dict:
    if not isinstance(speed_mps, (int, float)) or speed_mps <= 0:
        return {}
    if any(type_key.startswith(t) or type_key == t for t in _PACE_TYPES):
        seconds = 1000 / speed_mps
        return {"pace_min_per_km": f"{int(seconds // 60)}:{int(round(seconds % 60)):02d}"}
    return {"avg_speed_kmh": round(speed_mps * 3.6, 1)}


def _strength(sets: Any, reps: Any, exercises: Any = None) -> dict:
    """Sets, reps and exercise categories of a strength workout (``PULL_UP`` -> ``pull up``)."""
    names = []
    for item in exercises or []:
        category = item.get("category") if isinstance(item, Mapping) else None
        if category and category != "UNKNOWN":
            name = str(category).replace("_", " ").lower()
            if name not in names:
                names.append(name)
    return _clean({"sets": sets if isinstance(sets, int) and sets > 0 else None,
                   "reps": reps if isinstance(reps, int) and reps > 0 else None,
                   "exercises": names})


def activity(dto: Mapping[str, Any]) -> dict:
    """``SummarizedActivityDTO`` (an item of the activity search)."""
    type_key = _get(dto, "activityType", "typeKey") or ""
    zones = {f"z{i}": hmm(dto.get(f"hrTimeInZone_{i}")) for i in range(1, 6)}
    return _clean({
        "id": dto.get("activityId"),
        "name": dto.get("activityName"),
        "type": type_key,
        "type_label": activity_label(type_key),
        "start_local": local_time(dto.get("startTimeLocal")),
        "distance_km": _num((dto.get("distance") or 0) / 1000, 2) or None,
        "duration": hms(dto.get("duration")),
        "moving_duration": hms(dto.get("movingDuration")),
        **_pace_or_speed(type_key, dto.get("averageSpeed")),
        "avg_run_cadence_spm": _num(dto.get("averageRunningCadenceInStepsPerMinute")),
        **_strength(dto.get("activeSets"), dto.get("totalReps"), dto.get("summarizedExerciseSets")),
        "elevation_gain_m": _num(dto.get("elevationGain")),
        "avg_hr_bpm": _num(dto.get("averageHR")),
        "max_hr_bpm": _num(dto.get("maxHR")),
        "calories_kcal": _num(dto.get("calories")),
        "avg_power_w": _num(dto.get("avgPower")),
        "aerobic_te": _num(dto.get("aerobicTrainingEffect"), 1),
        "anaerobic_te": _num(dto.get("anaerobicTrainingEffect"), 1),
        "training_load": _num(dto.get("activityTrainingLoad")),
        "vo2max": _num(dto.get("vO2MaxValue"), 1),
        "body_battery_impact": dto.get("differenceBodyBattery"),
        "hr_zones_hmm": zones,
        "personal_record": True if dto.get("isPR") or dto.get("pr") else None,
    })


def activity_detail(dto: Mapping[str, Any]) -> dict:
    """``ActivitySummaryDTO`` (``GET /activity/{id}``); field names differ from the list DTO."""
    summary = dto.get("summaryDTO") if isinstance(dto.get("summaryDTO"), Mapping) else {}
    type_key = _get(dto, "activityTypeDTO", "typeKey") or ""
    splits = []
    for split in dto.get("splitSummaries") or []:
        if isinstance(split, Mapping):
            splits.append(_clean({
                "type": split.get("splitType"),
                "count": split.get("noOfSplits"),
                "distance_km": _num((split.get("distance") or 0) / 1000, 2) or None,
                "duration": hms(split.get("duration")),
            }))
    return _clean({
        "id": dto.get("activityId"),
        "name": dto.get("activityName"),
        "description": dto.get("description"),
        "type": type_key,
        "type_label": activity_label(type_key),
        "event_type": _get(dto, "eventTypeDTO", "typeKey"),
        "location": dto.get("locationName"),
        "time_zone": _get(dto, "timeZoneUnitDTO", "timeZone"),
        "device": _get(dto, "metadataDTO", "manufacturer"),
        "file_format": _get(dto, "metadataDTO", "fileFormat", "formatKey"),
        "start_local": local_time(summary.get("startTimeLocal")),
        "distance_km": _num((summary.get("distance") or 0) / 1000, 2) or None,
        "duration": hms(summary.get("duration")),
        "moving_duration": hms(summary.get("movingDuration")),
        "elapsed_duration": hms(summary.get("elapsedDuration")),
        **_pace_or_speed(type_key, summary.get("averageSpeed")),
        "elevation_gain_m": _num(summary.get("elevationGain")),
        "elevation_loss_m": _num(summary.get("elevationLoss")),
        "avg_hr_bpm": _num(summary.get("averageHR")),
        "max_hr_bpm": _num(summary.get("maxHR")),
        "recovery_hr_bpm": summary.get("recoveryHeartRate"),
        "avg_run_cadence_spm": _num(summary.get("averageRunCadence")),
        "avg_bike_cadence_rpm": _num(summary.get("averageBikeCadence")),
        "avg_power_w": _num(summary.get("averagePower")),
        "normalized_power_w": _num(summary.get("normalizedPower")),
        "calories_kcal": _num(summary.get("calories")),
        "aerobic_te": _num(summary.get("trainingEffect"), 1),
        "anaerobic_te": _num(summary.get("anaerobicTrainingEffect"), 1),
        "training_effect_label": summary.get("trainingEffectLabel"),
        "training_load": _num(summary.get("activityTrainingLoad")),
        "training_stress_score": _num(summary.get("trainingStressScore"), 1),
        "ground_contact_ms": _num(summary.get("groundContactTime")),
        "stride_length_cm": _num(summary.get("strideLength")),
        "vertical_oscillation_cm": _num(summary.get("verticalOscillation"), 1),
        "avg_respiration_brpm": _num(summary.get("avgRespirationRate"), 1),
        "avg_temperature_c": _num(summary.get("averageTemperature"), 1),
        "steps": summary.get("steps"),
        **_strength(summary.get("activeSets"), summary.get("totalExerciseReps")),
        "intensity_minutes": _intensity(summary.get("moderateIntensityMinutes"),
                                        summary.get("vigorousIntensityMinutes")),
        "body_battery_impact": summary.get("differenceBodyBattery"),
        "stamina_start_end_pct": (f"{_num(summary['beginPotentialStamina'])}-{_num(summary['endPotentialStamina'])}"
                                  if summary.get("beginPotentialStamina") is not None
                                  and summary.get("endPotentialStamina") is not None else None),
        "perceived_effort_rpe": summary.get("directWorkoutRpe"),
        "feel": summary.get("directWorkoutFeel"),
        "splits": splits,
    })


def sorted_by_date(items: Iterable[dict], key: str = "date") -> list[dict]:
    return sorted((i for i in items if i), key=lambda item: str(item.get(key, "")))


def parse_iso_day(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
