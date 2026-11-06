"""Model-facing tools. All read-only; each returns a JSON string and never raises."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from . import endpoints, reports, summaries
from .client import ApiError, GarminError
from .dates import ArgError, parse_day, resolve_range
from .runtime import RUNTIME

logger = logging.getLogger(__name__)

MAX_RANGE_DAYS = 93
FIT_MAX_BYTES = 50_000_000

_DATE = {"type": "string", "description": "A single day, YYYY-MM-DD, 'today' or 'yesterday'. Default today."}
_START = {"type": "string", "description": "First day of a range (YYYY-MM-DD). Use with end_date."}
_END = {"type": "string", "description": "Last day of a range (YYYY-MM-DD), inclusive. Default today."}
_DAYS = {"type": "integer", "minimum": 1, "maximum": MAX_RANGE_DAYS,
         "description": "Instead of dates: the last N days ending today."}
_RAW = {"type": "boolean", "description": "Also return Garmin's raw payload. Large; only when the summary lacks "
        "a field you need."}
_RANGE = {"date": _DATE, "start_date": _START, "end_date": _END, "days": _DAYS}
_ID = {"type": "integer", "description": "Garmin activity ID (from garmin_activities)."}


def _schema(name: str, description: str, properties: dict, required: tuple = ()) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}


def _tool(fn: Callable[[dict], dict]) -> Callable[..., str]:
    # Not functools.wraps: Hermes reads the handler signature, which must keep ``**kwargs``.
    def handler(args: dict | None = None, **_kwargs: Any) -> str:
        try:
            result = fn(dict(args or {}))
        except (ArgError, GarminError) as exc:
            result = {"error": str(exc)}
        except (TypeError, ValueError) as exc:
            result = {"error": f"Invalid argument: {exc}"}
        except Exception as exc:
            logger.exception("garmin tool %s failed", fn.__name__)
            result = {"error": f"Unexpected error in the Garmin plugin ({type(exc).__name__})."}
        return json.dumps(result, ensure_ascii=False)
    handler.__name__ = handler.__qualname__ = fn.__name__
    handler.__doc__ = fn.__doc__
    return handler


def _range(args: dict, default_days: int = 1, max_days: int = MAX_RANGE_DAYS):
    return resolve_range(args, RUNTIME.today(), default_days=default_days, max_days=max_days)


def _with_raw(result: dict, args: dict, raw: Any) -> dict:
    if args.get("raw"):
        result["raw"] = raw
    return result


def _dicts(rows: Any) -> list[dict]:
    return [r for r in rows or [] if isinstance(r, dict)]


def _avg(values: list) -> Any:
    numbers = [v for v in values if isinstance(v, (int, float))]
    return round(mean(numbers)) if numbers else None


# -- handlers -----------------------------------------------------------------------------------

@_tool
def today(args: dict) -> dict:
    day = parse_day(args.get("date") or "today", RUNTIME.today())
    return reports.today(RUNTIME.client(), day)


@_tool
def sleep(args: dict) -> dict:
    start, end = _range(args)
    rows = _dicts(endpoints.sleep(RUNTIME.client(), start, end))
    nights = summaries.sorted_by_date(summaries.sleep(r) for r in rows)
    result: dict = {"nights": nights}
    if len(nights) > 1:
        seconds = [r.get("sleepTimeSeconds") for r in rows if (r.get("sleepTimeSeconds") or 0) > 0]
        result["average"] = {"total_sleep_hmm": summaries.hmm(_avg(seconds)) if seconds else None,
                             "score": _avg([n.get("score") for n in nights])}
    if not nights:
        result["note"] = f"No sleep recorded for {start}..{end}."
    return _with_raw(result, args, rows)


@_tool
def hrv(args: dict) -> dict:
    start, end = _range(args)
    rows = _dicts(endpoints.hrv(RUNTIME.client(), start, end))
    result: dict = {"days": summaries.sorted_by_date(summaries.hrv(r) for r in rows)}
    if not rows:
        result["note"] = "No HRV data (HRV is measured overnight and needs a compatible watch worn during sleep)."
    return _with_raw(result, args, rows)


@_tool
def body_battery(args: dict) -> dict:
    start, end = _range(args)
    rows = _dicts(endpoints.body_battery(RUNTIME.client(), start, end))
    tz = RUNTIME.tz()
    events = start == end or bool(args.get("include_events"))
    days = summaries.sorted_by_date(summaries.body_battery(r, tz, events=events) for r in rows)
    return _with_raw({"days": days}, args, rows)


@_tool
def stress(args: dict) -> dict:
    start, end = _range(args)
    rows = _dicts(endpoints.stress(RUNTIME.client(), start, end))
    days = summaries.sorted_by_date(summaries.stress(r) for r in rows)
    result: dict = {"scale": "0-100: 0-25 rest, 26-50 low, 51-75 medium, 76-100 high", "days": days}
    if len(days) > 1:
        result["average_stress"] = _avg([d.get("avg_stress") for d in days])
    return _with_raw(result, args, rows)


@_tool
def steps(args: dict) -> dict:
    start, end = _range(args)
    rows = _dicts(endpoints.steps(RUNTIME.client(), start, end))
    days = summaries.sorted_by_date(summaries.steps(r) for r in rows)
    result: dict = {"days": days}
    if len(days) > 1:
        counts = [d["steps"] for d in days if isinstance(d.get("steps"), int)]
        result["total_steps"] = sum(counts)
        result["daily_average"] = _avg(counts)
        result["days_goal_met"] = sum(1 for d in days if (d.get("goal_pct") or 0) >= 100)
    return _with_raw(result, args, rows)


@_tool
def heart_rate(args: dict) -> dict:
    start, end = _range(args)
    client = RUNTIME.client()
    if start == end:
        raw = endpoints.heart_rate(client, start)
        if not isinstance(raw, dict):
            return {"note": f"No heart rate data for {start}."}
        return _with_raw(summaries.heart_rate(raw, RUNTIME.tz(), hourly=bool(args.get("hourly"))), args, raw)
    rows = _dicts(endpoints.daily_summaries(client, start, end))
    keys = ("date", "resting_hr_bpm", "min_hr_bpm", "max_hr_bpm")
    days = summaries.sorted_by_date(
        {k: v for k, v in summaries.daily_summary_v2(r).items() if k in keys} for r in rows)
    result = {"days": days, "average_resting_hr_bpm": _avg([d.get("resting_hr_bpm") for d in days])}
    return _with_raw(result, args, rows)


@_tool
def activities(args: dict) -> dict:
    limit = max(1, min(int(args.get("limit") or 10), 100))
    start = end = None
    if any(args.get(k) for k in ("start_date", "end_date", "days", "date")):
        start, end = _range(args, max_days=3660)
    rows = _dicts(endpoints.activities(
        RUNTIME.client(), limit=limit, start=start, end=end,
        activity_type=str(args.get("activity_type") or "").strip(), search=str(args.get("search") or "").strip()))
    result: dict = {"activities": [summaries.activity(r) for r in rows]}
    if len(rows) == limit:
        result["note"] = f"Showing the newest {limit}; narrow the dates or raise limit for more."
    return _with_raw(result, args, rows)


def _activity_id(args: dict) -> int:
    try:
        value = int(args.get("activity_id"))
    except (TypeError, ValueError):
        raise ArgError("activity_id must be a Garmin activity ID (a positive integer).") from None
    if value <= 0:
        raise ArgError("activity_id must be a positive integer.")
    return value


@_tool
def activity(args: dict) -> dict:
    raw = endpoints.activity(RUNTIME.client(), _activity_id(args))
    if not isinstance(raw, dict):
        return {"error": "Garmin returned no data for this activity."}
    return _with_raw(summaries.activity_detail(raw), args, raw)


def _download_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    target = plugin_data_dir("garmin-connect") / "activity-files"
    target.mkdir(parents=True, exist_ok=True)
    return target


@_tool
def activity_fit(args: dict) -> dict:
    activity_id = _activity_id(args)
    dest = _download_dir() / f"{activity_id}.zip"
    if dest.exists() and dest.stat().st_size > 0:
        return {"path": str(dest), "bytes": dest.stat().st_size, "cached": True}
    size = RUNTIME.client().download(endpoints.activity_file_path(activity_id), dest, max_bytes=FIT_MAX_BYTES)
    with dest.open("rb") as fh:
        is_zip = fh.read(2) == b"PK"
    if not is_zip:
        dest.unlink(missing_ok=True)
        raise ApiError("Garmin did not return a ZIP archive for this activity.")
    return {"path": str(dest), "bytes": size,
            "content": "ZIP with the original file (usually <id>_ACTIVITY.fit; .gpx or .tcx for imported activities)"}


@_tool
def body(args: dict) -> dict:
    start, end = _range(args, default_days=30, max_days=366)
    client = RUNTIME.client()
    weights = _dicts(endpoints.weight(client, start, end))
    water = _dicts(endpoints.hydration(client, start, end))
    result = {
        "range": f"{start}..{end}",
        "weight": summaries.sorted_by_date(summaries.weight(w) for w in weights),
        "hydration": [h for h in summaries.sorted_by_date(summaries.hydration(d) for d in water)
                      if h.get("intake_ml")],
    }
    if not result["weight"]:
        result["note"] = "No weight entries in this range (they come from a scale or manual entries)."
    return _with_raw(result, args, {"weight": weights, "hydration": water})


@_tool
def trends(args: dict) -> dict:
    days = max(14, min(int(args.get("days") or 28), 120))
    threshold = float(args.get("threshold_pct") or 10.0)
    as_of = RUNTIME.today()
    if args.get("as_of"):
        as_of = min(parse_day(args["as_of"], as_of), as_of)
    return reports.trend_report(RUNTIME.client(), as_of, days, flag_pct=max(1.0, threshold))


# -- registration table ---------------------------------------------------------------------------

TOOLS: tuple[tuple[str, dict, Callable[..., str], str], ...] = (
    ("garmin_today", _schema(
        "garmin_today",
        "Today's (or one day's) overview from the user's Garmin watch: last night's sleep and sleep score, "
        "overnight HRV and its status, Body Battery (now, at wake, high/low), stress, steps vs goal, resting "
        "heart rate and the most recent activity. Start here for 'how did I sleep', 'am I recovered', "
        "'should I train today'. Today's totals stop at the watch's last sync (synced_local); when "
        "freshness_note is present, low steps or stress mean an old sync, not inactivity; day_totals_note means "
        "Garmin has not finished processing the newest sync.",
        {"date": _DATE}), today, "⌚"),
    ("garmin_sleep", _schema(
        "garmin_sleep",
        "Sleep per night: total, stages (deep/light/REM/awake), sleep score, bedtime and wake time, overnight "
        "heart rate, respiration, SpO2. A night is filed under the date the user woke up. Ranges up to 93 days.",
        {**_RANGE, "raw": _RAW}), sleep, "😴"),
    ("garmin_hrv", _schema(
        "garmin_hrv",
        "Overnight heart rate variability (RMSSD, ms) per day with Garmin's HRV status (BALANCED, UNBALANCED, "
        "LOW, POOR) and the user's personal balanced range. The status rates the 7-day average, not last "
        "night: last_night_vs_range tells where the single night falls. HRV is individual: compare with the "
        "user's own baseline, not population norms.",
        {**_RANGE, "raw": _RAW}), hrv, "💓"),
    ("garmin_body_battery", _schema(
        "garmin_body_battery",
        "Body Battery energy estimate (0-100) per day: charged, drained, highest, lowest and latest (from a "
        "few points per day), plus the events that moved it when Garmin includes them.",
        {**_RANGE, "include_events": {"type": "boolean", "description": "Events for every day of a range."},
         "raw": _RAW}), body_battery, "🔋"),
    ("garmin_stress", _schema(
        "garmin_stress",
        "Garmin stress score (0-100, from heart rate variability while awake) per day with time in high, "
        "medium, low and rest stress.",
        {**_RANGE, "raw": _RAW}), stress, "🧘"),
    ("garmin_steps", _schema(
        "garmin_steps",
        "Daily steps, step goal and distance; totals and average for ranges.",
        {**_RANGE, "raw": _RAW}), steps, "👣"),
    ("garmin_heart_rate", _schema(
        "garmin_heart_rate",
        "Heart rate. One day: resting, min, max, 7-day resting average and optionally the hourly mean. A "
        "range: resting/min/max per day.",
        {**_RANGE, "hourly": {"type": "boolean", "description": "Single day only: mean bpm per local hour."},
         "raw": _RAW}), heart_rate, "❤️"),
    ("garmin_activities", _schema(
        "garmin_activities",
        "List or search the user's recorded activities (newest first): type, date, distance, duration, pace "
        "or speed, heart rate, training effect and load, HR zones. Filter by dates, type or text.",
        {**_RANGE,
         "activity_type": {"type": "string", "description": "Type key: running, cycling, swimming, walking, "
                           "hiking, fitness_equipment (gym), or a specific one such as strength_training, "
                           "indoor_cycling, treadmill_running. A type Garmin's filter rejects is matched "
                           "among the newest 400 activities."},
         "search": {"type": "string", "description": "Text in the activity name or description."},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Default 10."},
         "raw": _RAW}), activities, "🏃"),
    ("garmin_activity", _schema(
        "garmin_activity",
        "Full detail of one activity by ID: pace, heart rate, cadence, power, running dynamics, training "
        "effect and load, stamina, perceived effort, temperature and split summaries.",
        {"activity_id": _ID, "raw": _RAW}, ("activity_id",)), activity, "📊"),
    ("garmin_activity_fit", _schema(
        "garmin_activity_fit",
        "Download one activity's original file (a ZIP, usually containing a .fit) into the Hermes plugin "
        "data folder and return its local path, for lap or second-by-second analysis with other tools.",
        {"activity_id": _ID}, ("activity_id",)), activity_fit, "💾"),
    ("garmin_body", _schema(
        "garmin_body",
        "Body composition and hydration: weight (kg), BMI, body fat, water, muscle and bone mass per day, and "
        "logged water intake. Default the last 30 days; up to 366.",
        {"start_date": _START, "end_date": _END,
         "days": {**_DAYS, "maximum": 366}, "raw": _RAW}), body, "⚖️"),
    ("garmin_trends", _schema(
        "garmin_trends",
        "Deterministic baselines for HRV, resting heart rate, sleep, sleep score, stress, Body Battery, steps "
        "and intensity minutes over the last N days: mean, 7-day vs 28-day change, z-score of the latest "
        "value against the user's own baseline, and plain-language flags such as 'overnight HRV 18% below "
        "baseline'. Call this before drawing conclusions about recovery, fatigue or progress.",
        {"days": {"type": "integer", "minimum": 14, "maximum": 120, "description": "Window, default 28."},
         "threshold_pct": {"type": "number", "description": "Minimum % change to flag, default 10."},
         "as_of": {"type": "string", "description": "Analyse as of this day instead of today (YYYY-MM-DD)."}}),
     trends, "📈"),
)
