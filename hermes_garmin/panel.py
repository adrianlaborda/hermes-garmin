"""Payloads for the Hermes Desktop panel (``dashboard/plugin_api.py`` serves them).

Read-only, and built from the same reports and trend analysis the tools use. Every function
returns a JSON-ready dict with a ``state``: ``ok``, ``not_configured``, ``auth_error``,
``rate_limited`` or ``error``, so the panel can render onboarding or an error instead of
receiving an HTTP failure. Nothing returned ever contains the API key or the access token.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from functools import wraps
from statistics import mean
from typing import Any, Callable, Optional

from . import __version__, endpoints, reports, summaries, training, trends
from .client import (KEY_ENV, KEY_PAGE, AuthError, GarminError, NotConfiguredError, NotFoundError,
                     RateLimitedError, jwt_expiry)
from .runtime import PLUGIN_ID, Runtime

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 28
NEUTRAL_BAND_PCT = 5.0  # smaller changes against the baseline are shown as "steady"

# Friendly names accepted by ``/series`` besides the canonical metric keys.
METRIC_ALIASES = {
    "hrv": "hrv_ms",
    "rhr": "resting_hr_bpm",
    "resting_hr": "resting_hr_bpm",
    "sleep": "sleep_hours",
    "stress": "avg_stress",
    "body_battery": "body_battery_high",
    "intensity": "intensity_minutes",
}


def _envelope(state: str, message: str = "", **extra: Any) -> dict:
    out: dict = {"state": state}
    if message:
        out["message"] = message
    out.update(extra)
    return out


def guarded(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Turn Garmin failures into a renderable ``state``; never raise into the web server."""
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        try:
            return fn(*args, **kwargs)
        except NotConfiguredError as exc:
            return _envelope("not_configured", str(exc), key_page=KEY_PAGE, key_env=KEY_ENV)
        except AuthError as exc:
            return _envelope("auth_error", str(exc), key_page=KEY_PAGE, key_env=KEY_ENV)
        except RateLimitedError as exc:
            return _envelope("rate_limited", str(exc))
        except GarminError as exc:
            return _envelope("error", str(exc))
        except (TypeError, ValueError) as exc:
            return _envelope("error", f"Invalid request: {exc}")
        except Exception as exc:  # pragma: no cover - defensive, logged with the traceback
            logger.exception("garmin panel %s failed", fn.__name__)
            return _envelope("error", f"Unexpected error in the Garmin plugin ({type(exc).__name__}).")
    return wrapper


def _clamp_days(days: Any, low: int, high: int) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        value = DEFAULT_DAYS
    return max(low, min(value, high))


def metric_name(name: str) -> str:
    key = METRIC_ALIASES.get(str(name or "").strip().lower(), str(name or "").strip().lower())
    if key not in trends.METRICS:
        known = ", ".join(sorted({*trends.METRICS, *METRIC_ALIASES}))
        raise ValueError(f"unknown metric {name!r}; use one of: {known}")
    return key


def _points(points: list[tuple[date, float]]) -> list[list]:
    by_day = {day: value for day, value in points}
    return [[day.isoformat(), round(float(by_day[day]), 2)] for day in sorted(by_day)]


def _ready(rt: Runtime) -> None:
    """Fail fast with one clear state (no key, key rejected, rate limit) before fetching parts,
    which would otherwise each report the same failure."""
    if not rt.has_key():
        raise NotConfiguredError(f"No Garmin Personal API key is configured. Create one at {KEY_PAGE} and "
                                 f"store it as {KEY_ENV}.")
    rt.client().token()


# -- routes ---------------------------------------------------------------------------------------

def status(rt: Runtime, *, check: bool = False) -> dict:
    """Key configured, key expiry (when the key carries one) and, with ``check``, a token exchange."""
    key = rt.api_key()
    out = _envelope("ok", plugin=PLUGIN_ID, version=__version__, region=rt.region, key_configured=bool(key),
                    key_page=KEY_PAGE, key_env=KEY_ENV)
    if not key:
        out.update(state="not_configured",
                   message=f"No Garmin Personal API key is configured. Create one at {KEY_PAGE} and store "
                           f"it as {KEY_ENV}.")
        return out
    expiry = jwt_expiry(key)
    del key
    choice = rt.zone_choice()
    out.update(timezone=choice.name, timezone_source=choice.source)
    if expiry:
        out["key_expires_at"] = datetime.fromtimestamp(expiry, rt.tz()).isoformat(timespec="minutes")
        out["key_days_left"] = int((expiry - datetime.now().timestamp()) // 86400)
    if check:
        try:
            token = rt.client().token()
        except (AuthError, NotConfiguredError) as exc:
            out.update(state="auth_error", message=str(exc), token_ok=False)
        except GarminError as exc:
            out.update(state="error", message=str(exc), token_ok=False)
        else:
            out["token_ok"] = True
            out["token_valid_until"] = datetime.fromtimestamp(token.expires_at, rt.tz()).isoformat(timespec="minutes")
    return out


@guarded
def today(rt: Runtime) -> dict:
    _ready(rt)
    report = reports.today(rt.client(), rt.today())
    return _envelope("ok", **report)


@guarded
def trend(rt: Runtime, days: Any = DEFAULT_DAYS, *, include_series: bool = True) -> dict:
    days = _clamp_days(days, 14, 120)
    _ready(rt)
    as_of = rt.today()
    series, failed = reports.collect_series(rt.client(), as_of - timedelta(days=days - 1), as_of)
    report = reports.trend_report(rt.client(), as_of, days, series=series, failed=failed)
    out = _envelope("ok", **report)
    out["signals"] = signals(report)
    if include_series:
        out["series"] = {name: _points(points) for name, points in series.items() if points}
    return out


@guarded
def series(rt: Runtime, metric: str, days: Any = DEFAULT_DAYS) -> dict:
    name = metric_name(metric)
    days = _clamp_days(days, 7, 120)
    _ready(rt)
    as_of = rt.today()
    collected, failed = reports.collect_series(rt.client(), as_of - timedelta(days=days - 1), as_of)
    spec = trends.METRICS[name]
    out = _envelope("ok", metric=name, label=spec.label, unit=spec.unit, better=spec.better, days=days,
                    as_of=as_of.isoformat(), points=_points(collected.get(name) or []))
    if failed:
        out["unavailable"] = failed
    return out


@guarded
def overview(rt: Runtime, days: Any = DEFAULT_DAYS, *, fresh: bool = False) -> dict:
    """Everything the panel shows, in one request: status, metric cards with sparklines,
    signals and the last activity. ``fresh`` skips the client's short response cache."""
    days = _clamp_days(days, 14, 120)
    current = status(rt)
    if current["state"] != "ok":
        return current
    _ready(rt)
    client = rt.client()
    as_of = rt.today()
    kw = {"ttl": 0} if fresh else {}
    fetched, failed = reports._gather({
        "today": lambda: reports.today(client, as_of, **kw),
        "series": lambda: reports.collect_series(client, as_of - timedelta(days=days - 1), as_of, **kw),
        "fitness_age": lambda: _fitness_age_dto(client, as_of, **kw),
    })
    day_report = fetched.get("today") or {"date": as_of.isoformat()}
    collected, series_failed = fetched.get("series") or ({name: [] for name in trends.METRICS}, {})
    report = reports.trend_report(client, as_of, days, series=collected, failed=series_failed)
    unavailable = {**(day_report.get("unavailable") or {}), **series_failed, **failed}
    out = _envelope(
        "ok",
        date=as_of.isoformat(),
        generated_at=datetime.now(rt.tz()).isoformat(timespec="minutes"),
        window_days=days,
        status=current,
        cards=cards(day_report, collected, as_of, days),
        signals=signals(report),
        last_activity=last_activity(day_report.get("latest_activity")),
    )
    age = fitness_age_card(fetched.get("fitness_age"))
    if age:
        out["fitness_age"] = age
    for key in ("synced_local", "sync_age_hours", "freshness_note", "day_totals_as_of", "day_totals_note"):
        if day_report.get(key) is not None:
            out[key] = day_report[key]
    if unavailable:
        out["unavailable"] = unavailable
    return out


# -- building blocks ------------------------------------------------------------------------------

def _baseline(points: list[tuple[date, float]], as_of: date, days: int) -> Optional[float]:
    earlier = [v for d, v in points if as_of - timedelta(days=days) <= d < as_of]
    return mean(earlier) if len(earlier) >= trends.MIN_BASELINE_DAYS else None


def _tone(delta_pct: Optional[float], better: str) -> str:
    if delta_pct is None or not better or abs(delta_pct) < NEUTRAL_BAND_PCT:
        return "neutral"
    return "good" if (delta_pct > 0) == (better == "higher") else "bad"


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _card(card_id: str, label: str, metric: str, value: Any, collected: dict, as_of: date, days: int, *,
          display: Optional[str] = None, unit: str = "", detail: str = "", compare: Any = None) -> dict:
    """One metric card. ``compare`` is the value measured against the baseline (default ``value``).

    Today's running totals (``reports.CUMULATIVE_METRICS``) get no baseline badge: the day is not
    over, and ``garmin_trends`` (the Signals list) compares them on complete days only."""
    spec = trends.METRICS[metric]
    partial = metric in reports.CUMULATIVE_METRICS
    points = sorted(collected.get(metric) or [])
    number = _num(value)
    measured = _num(compare if compare is not None else value)
    baseline = _baseline(points, as_of, days)
    delta = (round(100 * (measured - baseline) / baseline, 1)
             if measured is not None and baseline and not partial else None)
    spark = _points(points)
    if measured is not None and (not spark or spark[-1][0] != as_of.isoformat()):
        spark.append([as_of.isoformat(), round(measured, 2)])
    card = {
        "id": card_id, "label": label, "metric": metric, "better": spec.better, "unit": unit,
        "value": number,
        "display": display if display is not None else (f"{number:,.0f}" if number is not None else None),
        "detail": detail,
        "baseline": round(baseline, 1) if baseline is not None else None,
        "delta_pct": delta,
        "tone": _tone(delta, spec.better),
        "partial": partial,
        "series": spark,
    }
    if delta is not None:
        sign = "+" if delta > 0 else "−" if delta < 0 else "±"
        card["badge"] = f"{sign}{abs(delta):.0f}%"
        card["badge_label"] = f"{sign}{abs(delta):.0f}% vs your {days}-day baseline"
    return card


def _title(text: Any) -> str:
    return str(text or "").replace("_", " ").strip().capitalize()


def hrv_detail(hrv: dict) -> str:
    """Last night against the balanced range, then Garmin's status, which rates the 7-day average:
    ``Below your 45–60 ms range · 7-day avg 52 ms, Balanced``."""
    rng = (hrv.get("balanced_range_ms") or "").replace("-", "–")
    position = hrv.get("last_night_vs_range")
    label = summaries.hrv_status_label(hrv.get("status"))
    bits = []
    if position and rng:
        bits.append(f"{position.capitalize()} your {rng} ms range")
    weekly = hrv.get("weekly_avg_ms")
    if weekly is not None:
        bits.append(f"7-day avg {weekly} ms" + (f", {label}" if label else ""))
    elif label:
        bits.append(label)
    if not position and rng:
        bits.append(f"range {rng} ms")
    return " · ".join(bits)


def cards(day_report: dict, collected: dict, as_of: date, days: int = DEFAULT_DAYS) -> list[dict]:
    sleep = day_report.get("sleep") or {}
    hrv = day_report.get("hrv") or {}
    day = day_report.get("day") or {}
    battery = day.get("body_battery") or {}
    out = []

    # Sleep: the score when the watch gives one, else hours.
    quality = _title(sleep.get("score_quality"))
    hours = sleep.get("total_sleep_hmm")
    if sleep.get("score") is not None or not (collected.get("sleep_hours") and hours):
        out.append(_card("sleep", "Sleep", "sleep_score", sleep.get("score"), collected, as_of, days,
                         detail=" · ".join(p for p in (f"{hours} h" if hours else "", quality) if p)))
    else:
        h, m = (int(x) for x in hours.split(":"))
        out.append(_card("sleep", "Sleep", "sleep_hours", h + m / 60, collected, as_of, days,
                         display=hours, unit="h", detail="no sleep score"))

    out.append(_card("hrv", "Overnight HRV", "hrv_ms", hrv.get("last_night_avg_ms"), collected, as_of, days,
                     unit="ms", detail=hrv_detail(hrv)))

    high_low = " ".join(f"{arrow}{battery[k]}" for arrow, k in (("↑", "highest"), ("↓", "lowest"))
                        if battery.get(k) is not None)
    battery_bits = [high_low, f"wake {battery['at_wake']}" if battery.get("at_wake") is not None else ""]
    # Sparkline: daily peaks on complete days, then today's peak so far.
    out.append(_card("body_battery", "Body Battery", "body_battery_high", battery.get("latest"), collected, as_of,
                     days, detail=" · ".join(b for b in battery_bits if b), compare=battery.get("highest")))

    # Today's running totals stop at the last sync; say so when it is hours old.
    so_far = f"as of {day_report['synced_local'][11:16]} sync" if day_report.get("freshness_note") else "so far"
    stress_bits = [so_far, f"max {day['max_stress']}" if day.get("max_stress") is not None else ""]
    out.append(_card("stress", "Stress", "avg_stress", day.get("avg_stress"), collected, as_of, days,
                     detail=" · ".join(b for b in stress_bits if b)))

    rhr_avg = day.get("resting_hr_7d_avg_bpm")
    out.append(_card("resting_hr", "Resting HR", "resting_hr_bpm", day.get("resting_hr_bpm"), collected, as_of,
                     days, unit="bpm", detail=f"7-day avg {rhr_avg} bpm" if rhr_avg else ""))

    goal = day.get("step_goal")
    steps = _card("steps", "Steps", "steps", day.get("steps"), collected, as_of, days,
                  detail=f"{so_far} · goal {goal:,}" if goal else so_far)
    if steps["value"] is not None and goal:
        pct = round(100 * steps["value"] / goal)
        steps.update(goal=goal, goal_pct=pct, badge=f"{pct}%", badge_label=f"{pct}% of the step goal",
                     tone="good" if pct >= 100 else "neutral")
    out.append(steps)
    return out


def _as_day(text: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        return None


def _day_word(day_text: str, as_of: Optional[date]) -> str:
    day = _as_day(day_text)
    if day is None or as_of is None:
        return f"on {day_text}"
    if day == as_of:
        return "today"
    if day == as_of - timedelta(days=1):
        return "yesterday"
    return f"on {day.strftime('%b')} {day.day}"


def signals(report: dict) -> list[dict]:
    """Trend flags as short sentences: ``Overnight HRV 18% below your baseline``."""
    out = []
    metrics = report.get("metrics") or {}
    as_of = _as_day(report.get("as_of"))
    for flag in report.get("flags") or []:
        name = flag.get("metric")
        spec = trends.METRICS.get(name)
        stats = metrics.get(name) or {}
        if spec is None:
            continue
        label = spec.label[0].upper() + spec.label[1:]
        if flag.get("kind") == "latest_vs_baseline" and stats.get("latest_vs_baseline_pct") is not None:
            pct = stats["latest_vs_baseline_pct"]
            headline = f"{label} {abs(pct):.0f}% {'above' if pct > 0 else 'below'} your baseline"
            detail = (f"{trends._fmt(stats['latest'], spec)} {_day_word(stats['latest_date'], as_of)} vs "
                      f"{trends._fmt(stats['baseline_mean'], spec)} usual")
        elif flag.get("kind") == "7d_vs_28d" and stats.get("avg_7d_vs_28d_pct") is not None:
            pct = stats["avg_7d_vs_28d_pct"]
            headline = f"7-day {spec.label} {abs(pct):.0f}% {'above' if pct > 0 else 'below'} your 28-day average"
            detail = f"{trends._fmt(stats['avg_7d'], spec)} vs {trends._fmt(stats['avg_28d'], spec)}"
        else:
            headline, detail = flag.get("text", ""), ""
        tone = {"favourable": "good", "unfavourable": "bad"}.get(flag.get("verdict"), "neutral")
        out.append({"metric": name, "kind": flag.get("kind"), "tone": tone, "headline": headline,
                    "detail": detail, "text": flag.get("text", "")})
    return out


def _fitness_age_dto(client, day: date, **kw: Any) -> Optional[dict]:
    try:
        return endpoints.fitness_age(client, day, **kw)
    except NotFoundError:  # an account without fitness age: nothing to show, not a failure
        return None


def fitness_age_card(dto: Optional[dict]) -> Optional[dict]:
    """The panel's compact fitness-age row: ``35.4`` years, ``4.6 younger than your age of 40``."""
    summary = training.fitness_age(dto) if dto else {}
    value, age = summary.get("fitness_age"), summary.get("chronological_age")
    if value is None:
        return None
    out: dict = {"value": value, "display": f"{value:.1f}", "unit": "years", "tone": "neutral"}
    if age is not None:
        delta = summary.get("years_vs_age") or 0.0
        out["age"] = age
        out["badge"] = f"{'−' if delta < 0 else '+'}{abs(delta):.1f}" if abs(delta) >= 0.1 else "±0"
        out["badge_label"] = (f"{abs(delta):.1f} years {'younger' if delta < 0 else 'older'} than your age of {age}"
                              if abs(delta) >= 0.1 else f"the same as your age of {age}")
        out["tone"] = "good" if delta <= -1 else "neutral"
    bits = []
    if summary.get("achievable_fitness_age") is not None:
        bits.append(f"achievable {summary['achievable_fitness_age']:.1f}")
    change = summary.get("change_since_previous")
    if change:
        bits.append(f"{'−' if change < 0 else '+'}{abs(change):.1f} since the last update")
    out["detail"] = " · ".join(bits)
    return out


def last_activity(activity: Optional[dict]) -> Optional[dict]:
    if not activity:
        return None
    keys = ("id", "name", "type", "start_local", "distance_km", "duration", "moving_duration",
            "pace_min_per_km", "avg_speed_kmh", "avg_hr_bpm", "max_hr_bpm", "elevation_gain_m",
            "calories_kcal", "aerobic_te", "training_load", "sets", "reps", "personal_record")
    out = {k: activity[k] for k in keys if activity.get(k) is not None}
    out["type_label"] = activity.get("type_label") or summaries.activity_label(activity.get("type"))
    return out
