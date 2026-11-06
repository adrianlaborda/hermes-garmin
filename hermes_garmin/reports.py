"""Multi-endpoint reports: the daily overview and the trend analysis."""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from . import endpoints, summaries, trends
from .client import AuthError, GarminClient, GarminError, NotConfiguredError

# Running totals for the current day are incomplete, so trends compare them up to yesterday only.
CUMULATIVE_METRICS = {"steps", "intensity_minutes", "avg_stress", "body_battery_high"}
# Today's totals are called stale when the last sync is older than this.
STALE_SYNC = timedelta(hours=2)
# The day's running totals change with every sync; reuse them for a minute at most.
DAY_TTL_S = 60.0
# A newer end of wellness data than the daily summary's own sync means a lagging snapshot only past
# this gap: the data is filed in quarter hours.
SNAPSHOT_LAG_MIN = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _for_day(rows: list, day: date) -> Optional[dict]:
    """The row filed under ``day``, else the newest one."""
    rows = [r for r in rows or [] if isinstance(r, dict)]
    return next((r for r in rows if r.get("calendarDate") == day.isoformat()), rows[-1] if rows else None)


def _gather(jobs: dict[str, Callable[[], Any]]) -> tuple[dict[str, Any], dict[str, str]]:
    """Run independent fetches concurrently. A rejected or missing key fails the whole report;
    any other failure only marks that part unavailable. Each job runs in a copy of the caller's
    context, which carries Hermes' per-profile secret scope."""
    results: dict[str, Any] = {}
    failed: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(contextvars.copy_context().run, job) for name, job in jobs.items()}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except (AuthError, NotConfiguredError):
                raise
            except GarminError as exc:
                failed[name] = str(exc)
    return results, failed


def today(client: GarminClient, day: date, *, now: Optional[datetime] = None, **kw: Any) -> dict:
    """``now`` (aware; default the current time) decides whether today's totals are stale."""
    kw.setdefault("ttl", DAY_TTL_S)
    fetched, failed = _gather({
        "day": lambda: endpoints.daily_summary(client, day, **kw),
        "day_v2": lambda: endpoints.daily_summaries(client, day, day, **kw),
        "sleep": lambda: endpoints.sleep(client, day, day, **kw),
        "hrv": lambda: endpoints.hrv(client, day, day, **kw),
        "latest_activity": lambda: endpoints.activities(client, limit=1, **kw),
    })
    failed.pop("day_v2", None)  # only a second opinion on the sync time
    night = _for_day(fetched.get("sleep"), day)
    hrv_day = _for_day(fetched.get("hrv"), day)
    activities = fetched.get("latest_activity") or []
    raw_day = fetched.get("day") if isinstance(fetched.get("day"), dict) else None
    day_summary = summaries.daily_summary(raw_day) if raw_day else None
    report = {
        "date": day.isoformat(),
        "sleep": summaries.sleep(night) if night else None,
        "hrv": summaries.hrv(hrv_day) if hrv_day else None,
        "day": day_summary,
        "latest_activity": summaries.activity(activities[0]) if activities else None,
    }
    report = {k: v for k, v in report.items() if v}
    if raw_day:
        v2_day = _for_day(fetched.get("day_v2"), day)
        snapshot = (day_summary or {}).get("last_sync_local")
        synced = latest_sync(day, snapshot, raw_day.get("wellnessEndTimeLocal"),
                             summaries.wellness_end_local(v2_day) if v2_day else None)
        if snapshot and synced and _minutes_between(snapshot, synced) < SNAPSHOT_LAG_MIN:
            synced = snapshot  # the wellness data ends on a quarter hour, not at the sync itself
        report.update(_freshness(raw_day, synced, day, now))
        if snapshot and synced and synced > snapshot:
            # Garmin answers the daily summary from an older snapshot for a short while after a sync.
            report["day_totals_as_of"] = snapshot
            report["day_totals_note"] = (
                f"The watch synced at {synced[11:16]} but Garmin is still processing it: the day's totals "
                f"(steps, stress, Body Battery) are from {snapshot[11:16]}. Asking again in a minute gives "
                "the updated totals.")
    if failed:
        report["unavailable"] = failed
    return report


def _minutes_between(earlier: str, later: str) -> float:
    return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds() / 60


def latest_sync(day: date, *stamps: Optional[str]) -> Optional[str]:
    """The newest of several local ``YYYY-MM-DDTHH:MM`` sync estimates. The end of a day's wellness
    data counts only while it falls inside that day (a finished day ends at the next midnight)."""
    best: Optional[str] = None
    for index, stamp in enumerate(stamps):
        text = summaries.local_time(stamp) if stamp else None
        if not text:
            continue
        if index > 0 and summaries.parse_iso_day(text) != day:
            continue
        best = text if best is None or text > best else best
    return best


def _freshness(raw_day: dict, synced: Optional[str], day: date, now: Optional[datetime]) -> dict:
    """``synced_local`` and, when today's totals stop hours ago, a note so nobody reads a low step
    count as inactivity."""
    if not synced:
        return {}
    out: dict = {"synced_local": synced}
    offset = summaries.utc_offset(raw_day)
    if offset is None:
        return out
    local_now = ((now or _now()).astimezone(timezone.utc) + offset).replace(tzinfo=None)
    synced_at = datetime.fromisoformat(synced)
    if local_now.date() != day or local_now - synced_at < STALE_SYNC:
        return out
    hours = int((local_now - synced_at).total_seconds() // 3600)
    out["sync_age_hours"] = hours
    out["freshness_note"] = (
        f"The watch last synced at {synced[11:16]} local time ({hours} h ago). Today's steps, stress, "
        "Body Battery and intensity minutes stop at that sync, so low totals do not mean the user was "
        "inactive; say the data is from that time and suggest opening Garmin Connect to sync.")
    return out


def when_text(stamp: Any, today: date) -> str:
    """``2026-11-05T13:05`` as ``yesterday 13:05`` (``today``, a weekday name within the week,
    else ``Nov 5``)."""
    text = str(stamp or "")
    day = summaries.parse_iso_day(text)
    if day is None:
        return text
    time = text[11:16] if len(text) >= 16 else ""
    delta = (today - day).days
    if delta == 0:
        word = "today"
    elif delta == 1:
        word = "yesterday"
    elif 1 < delta < 7:
        word = day.strftime("%A")
    else:
        word = f"{day.strftime('%b')} {day.day}"
    return f"{word} {time}".strip()


def _short_duration(hms: Any) -> str:
    """``0:44:49`` -> ``45 min``; ``1:02:05`` -> ``1 h 02 min``."""
    try:
        h, m, sec = (int(x) for x in str(hms).split(":"))
    except ValueError:
        return str(hms or "")
    minutes = h * 60 + m + (1 if sec >= 30 else 0)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


def _hrv_text(hrv: dict) -> str:
    night, weekly = hrv.get("last_night_avg_ms"), hrv.get("weekly_avg_ms")
    label = summaries.hrv_status_label(hrv.get("status"))
    rng = (hrv.get("balanced_range_ms") or "").replace("-", "–")
    bits = []
    if weekly is not None:
        bits.append(f"7-day avg {weekly} ms" + (f" ({label})" if label else ""))
    elif label:
        bits.append(label)
    if night is not None:
        position = hrv.get("last_night_vs_range")
        where = (f", below your usual {rng} ms" if position == "below" else
                 f", above your usual {rng} ms" if position == "above" else
                 f", within your usual {rng} ms" if position == "within" else "")
        bits.append(f"last night {night} ms{where}")
    return "; ".join(bits)


def status_line(report: dict, limit: int = 640) -> str:
    """One short line for the chat context, or "" when there is nothing to say."""
    parts = []
    sleep, hrv, day, act = (report.get(k) or {} for k in ("sleep", "hrv", "day", "latest_activity"))
    if sleep.get("total_sleep_hmm"):
        score = f" (score {sleep['score']})" if sleep.get("score") is not None else ""
        parts.append(f"slept {sleep['total_sleep_hmm']} h{score}")
    if hrv.get("last_night_avg_ms") or hrv.get("weekly_avg_ms"):
        parts.append("HRV " + _hrv_text(hrv))
    battery = day.get("body_battery") or {}
    if battery.get("latest") is not None:
        wake = f", {battery['at_wake']} at wake" if battery.get("at_wake") is not None else ""
        parts.append(f"Body Battery {battery['latest']}{wake}")
    if day.get("resting_hr_bpm"):
        parts.append(f"resting HR {day['resting_hr_bpm']} bpm")
    if day.get("avg_stress") is not None:
        parts.append(f"avg stress {day['avg_stress']}")
    if day.get("steps") is not None:
        goal = f" of {day['step_goal']:,}" if day.get("step_goal") else ""
        parts.append(f"{day['steps']:,} steps{goal}")
    if report.get("synced_local"):
        stale = " (today's totals stop there)" if report.get("freshness_note") else ""
        parts.append(f"watch last synced {report['synced_local'][11:16]}{stale}")
    if act.get("type"):
        distance = f" {act['distance_km']} km" if act.get("distance_km") else ""
        label = act.get("type_label") or act["type"]
        parts.append(f"last activity {label}{distance} on {str(act.get('start_local', ''))[:10]}")
    if not parts:
        return ""
    line = (f"[Garmin status {report['date']}, from the user's own Garmin data: " + "; ".join(parts)
            + ". Use garmin_* tools for detail; not medical advice.]")
    return line if len(line) <= limit else line[: limit - 2] + "…]"


def human_text(report: dict) -> str:
    """Multi-line overview for the ``/garmin today`` command."""
    today_date = summaries.parse_iso_day(report.get("date")) or date.today()
    header = f"Garmin Connect, {report['date']}"
    if report.get("synced_local"):
        header += f" (watch synced {when_text(report['synced_local'], today_date)})"
    lines = [header]
    if report.get("freshness_note"):
        lines.append(f"Note: last sync {report.get('sync_age_hours', '?')} h ago; today's steps, stress and "
                     "Body Battery stop there. Open Garmin Connect on the phone to sync.")
    if report.get("day_totals_note"):
        lines.append(f"Note: Garmin is still processing the {report['synced_local'][11:16]} sync; the totals "
                     f"below are from {report['day_totals_as_of'][11:16]}. Try again in a minute.")
    sleep, hrv, day, act = (report.get(k) or {} for k in ("sleep", "hrv", "day", "latest_activity"))
    if sleep:
        stages = sleep.get("stages") or {}
        detail = ", ".join(f"{name} {s['hmm']}" for name, s in stages.items() if s.get("hmm"))
        score = f", score {sleep['score']}" if sleep.get("score") is not None else ""
        lines.append(f"Sleep: {sleep.get('total_sleep_hmm', '?')} h{score}" + (f" ({detail})" if detail else ""))
    if hrv:
        lines.append(f"HRV: {_hrv_text(hrv) or 'no reading'}")
    battery = day.get("body_battery") or {}
    if battery:
        bits = []
        if battery.get("latest") is not None:
            bits.append(f"{battery['latest']} latest")
        if battery.get("at_wake") is not None:
            bits.append(f"{battery['at_wake']} at wake")
        if battery.get("lowest") is not None and battery.get("highest") is not None:
            bits.append(f"range {battery['lowest']}–{battery['highest']}")
        if battery.get("charged") is not None:
            bits.append(f"+{battery['charged']} charged")
        if battery.get("drained") is not None:
            bits.append(f"−{battery['drained']} drained")
        lines.append("Body Battery: " + ", ".join(bits))
    if day.get("resting_hr_bpm"):
        avg = f" (7-day avg {day['resting_hr_7d_avg_bpm']})" if day.get("resting_hr_7d_avg_bpm") else ""
        lines.append(f"Resting HR: {day['resting_hr_bpm']} bpm{avg}")
    as_of = f" (as of {report['synced_local'][11:16]})" if report.get("freshness_note") else ""
    if day.get("avg_stress") is not None:
        lines.append(f"Stress: avg {day['avg_stress']}, max {day.get('max_stress', '?')}{as_of}")
    if day.get("steps") is not None:
        goal = f" / {day['step_goal']:,}" if day.get("step_goal") else ""
        lines.append(f"Steps: {day['steps']:,}{goal}{as_of}")
    if act:
        label = act.get("type_label") or summaries.activity_label(act.get("type"))
        title = f"{act['name']} ({label})" if act.get("name") and act["name"] != label else label
        bits = [title, when_text(act.get("start_local"), today_date)]
        bits += [f"{act['distance_km']} km"] if act.get("distance_km") else []
        bits += [_short_duration(act["duration"])] if act.get("duration") else []
        if act.get("sets"):
            bits.append(f"{act['sets']} sets" + (f", {act['reps']} reps" if act.get("reps") else ""))
        lines.append("Last activity: " + ", ".join(b for b in bits if b))
    for part, message in (report.get("unavailable") or {}).items():
        lines.append(f"{part}: unavailable ({message})")
    if len(lines) == 1:
        lines.append("No data for this day yet. Has the watch synced?")
    return "\n".join(lines)


def collect_series(client: GarminClient, start: date, as_of: date,
                   **kw: Any) -> tuple[dict[str, list], dict[str, str]]:
    """Daily ``(day, value)`` points per trend metric for ``start..as_of``, plus failed parts.

    Running totals of ``as_of`` itself (steps, stress, Body Battery peak, intensity minutes) are
    left out because the day is not over."""
    fetched, failed = _gather({
        "daily": lambda: endpoints.daily_summaries(client, start, as_of, **kw),
        "sleep": lambda: endpoints.sleep(client, start, as_of, **kw),
        "hrv": lambda: endpoints.hrv(client, start, as_of, **kw),
    })
    series: dict[str, list] = {name: [] for name in trends.METRICS}

    def add(name: str, day_text: Any, value: Any) -> None:
        day = summaries.parse_iso_day(day_text)
        if day is None or not isinstance(value, (int, float)) or isinstance(value, bool):
            return
        if name in CUMULATIVE_METRICS and day >= as_of:
            return
        series[name].append((day, value))

    for row in fetched.get("hrv") or []:
        if isinstance(row, dict):
            add("hrv_ms", row.get("calendarDate"), row.get("lastNightAvg"))
    for row in fetched.get("sleep") or []:
        if isinstance(row, dict):
            seconds = row.get("sleepTimeSeconds")
            if isinstance(seconds, (int, float)) and seconds > 0:
                add("sleep_hours", row.get("calendarDate"), seconds / 3600)
            add("sleep_score", row.get("calendarDate"), summaries.sleep_score(row))
    for row in fetched.get("daily") or []:
        if isinstance(row, dict):
            flat = summaries.daily_summary_v2(row)
            for name in ("resting_hr_bpm", "avg_stress", "body_battery_high", "steps", "intensity_minutes"):
                add(name, flat.get("date"), flat.get(name))
    return series, failed


def trend_report(client: GarminClient, as_of: date, days: int, *, flag_pct: float = 10.0,
                 flag_z: float = 1.0, series: dict[str, list] | None = None,
                 failed: dict[str, str] | None = None) -> dict:
    """Baselines and flags over the last ``days`` days. Pass ``series``/``failed`` from
    :func:`collect_series` to analyse data already fetched."""
    if series is None:
        series, failed = collect_series(client, as_of - timedelta(days=days - 1), as_of)
    report = trends.analyze({k: v for k, v in series.items() if v}, as_of, window_days=days,
                            flag_pct=flag_pct, flag_z=flag_z)
    missing = [name for name, points in series.items() if not points]
    if missing:
        report["no_data"] = missing
    if failed:
        report["unavailable"] = failed
    return report
