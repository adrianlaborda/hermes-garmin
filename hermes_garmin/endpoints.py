"""Garmin Personal API endpoints, returning raw DTOs. Shared by the tools, the hook and any dashboard.

Window sizes are what the live API accepted in November 2026 (longer windows got HTTP 400), with
headroom where it accepted much more: steps and stress 28 days, Body Battery, hydration and sleep
31 (sleep took 32, not 35), HRV and daily summaries up to 366, weight at least 1,100.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from .client import ApiError, GarminClient

MAX_DAYS = {
    "steps": 28,
    "stress": 28,
    "hydration": 31,
    "sleep": 31,
    "body_battery": 31,
    "daily_summaries": 120,  # 366 accepted, 400 rejected
    "hrv": 120,  # 366 accepted, 400 rejected
    "weight": 366,  # 1,100 accepted
}
# Pages of 100 worked; the community has reported HTTP 500 above ~45 items, so stay below that.
ACTIVITY_PAGE = 40
# Garmin's filter takes parent types (``fitness_equipment``) and some child types (``indoor_cycling``)
# but answers HTTP 400 to others (``strength_training``): those are matched here over at most this
# many of the newest activities.
TYPE_SCAN_LIMIT = 400


def _list(data: Any) -> list:
    return data if isinstance(data, list) else []


def user_settings(client: GarminClient, **kw) -> Optional[dict]:
    """``UserSettingsDTO``: units, locale and the profile's IANA ``timeZone``."""
    data = client.get("/userprofile-service/userprofile/settings", **kw)
    return data if isinstance(data, dict) else None


def daily_summary(client: GarminClient, day: date, **kw) -> Optional[dict]:
    return client.get("/usersummary-service/usersummary/daily", {"calendarDate": day.isoformat()}, **kw)


def daily_summaries(client: GarminClient, start: date, end: date, **kw) -> list:
    return client.get_range(
        lambda s, e: ("/usersummary-service/v2/dailysummaries",
                      {"startDate": s.isoformat(), "endDate": e.isoformat()}),
        start, end, MAX_DAYS["daily_summaries"],
        extract=lambda d: d.get("data") if isinstance(d, dict) else _list(d), **kw)


def sleep(client: GarminClient, start: date, end: date, **kw) -> list:
    return client.get_range(
        lambda s, e: ("/sleep-service/sleep/dailySleepSummariesByDate",
                      {"startDate": s.isoformat(), "endDate": e.isoformat()}),
        start, end, MAX_DAYS["sleep"], **kw)


def hrv(client: GarminClient, start: date, end: date, **kw) -> list:
    return client.get_range(
        lambda s, e: (f"/hrv-service/hrv/daily/{s.isoformat()}/{e.isoformat()}", None),
        start, end, MAX_DAYS["hrv"],
        extract=lambda d: d.get("hrvSummaries") if isinstance(d, dict) else _list(d), **kw)


def body_battery(client: GarminClient, start: date, end: date, **kw) -> list:
    return client.get_range(
        lambda s, e: ("/wellness-service/wellness/bodyBattery/reports/daily",
                      {"startDate": s.isoformat(), "endDate": e.isoformat()}),
        start, end, MAX_DAYS["body_battery"], **kw)


def stress(client: GarminClient, start: date, end: date, **kw) -> list:
    return client.get_range(
        lambda s, e: (f"/usersummary-service/stats/stress/daily/{s.isoformat()}/{e.isoformat()}", None),
        start, end, MAX_DAYS["stress"], **kw)


def steps(client: GarminClient, start: date, end: date, **kw) -> list:
    return client.get_range(
        lambda s, e: (f"/usersummary-service/stats/steps/daily/{s.isoformat()}/{e.isoformat()}", None),
        start, end, MAX_DAYS["steps"], **kw)


def heart_rate(client: GarminClient, day: date, **kw) -> Optional[dict]:
    return client.get("/wellness-service/wellness/dailyHeartRate", {"date": day.isoformat()}, **kw)


def weight(client: GarminClient, start: date, end: date, **kw) -> list:
    return client.get_range(
        lambda s, e: (f"/weight-service/weight/range/{s.isoformat()}/{e.isoformat()}", {"includeAll": "true"}),
        start, end, MAX_DAYS["weight"],
        extract=lambda d: d.get("dailyWeightSummaries") if isinstance(d, dict) else [], **kw)


def hydration(client: GarminClient, start: date, end: date, **kw) -> list:
    return client.get_range(
        lambda s, e: (f"/usersummary-service/stats/hydration/daily/{s.isoformat()}/{e.isoformat()}", None),
        start, end, MAX_DAYS["hydration"], **kw)


def activities(client: GarminClient, *, limit: int = 10, start: Optional[date] = None,
               end: Optional[date] = None, activity_type: str = "", search: str = "", **kw) -> list:
    """Newest first, paging with ``start``/``limit`` until ``limit`` items or a short page."""
    base: dict[str, Any] = {"sortOrder": "desc"}
    if start:
        base["startDate"] = start.isoformat()
    if end:
        base["endDate"] = end.isoformat()
    if search:
        base["search"] = search
    if not activity_type:
        return _pages(client, base, limit, **kw)
    try:
        return _pages(client, {**base, "activityType": activity_type}, limit, **kw)
    except ApiError as exc:
        if exc.status != 400:
            raise
    wanted = activity_type.strip().lower()
    matches: list = []
    scanned = 0
    while len(matches) < limit and scanned < TYPE_SCAN_LIMIT:
        batch = _list(client.get("/activitylist-service/activities/search/activities",
                                 {**base, "start": scanned, "limit": ACTIVITY_PAGE}, **kw))
        scanned += len(batch)
        matches.extend(a for a in batch if isinstance(a, dict)
                       and str((a.get("activityType") or {}).get("typeKey", "")).lower() == wanted)
        if len(batch) < ACTIVITY_PAGE:
            break
    return matches[:limit]


def _pages(client: GarminClient, params: dict, limit: int, **kw) -> list:
    items: list = []
    while len(items) < limit:
        page = min(ACTIVITY_PAGE, limit - len(items))
        batch = _list(client.get("/activitylist-service/activities/search/activities",
                                 {**params, "start": len(items), "limit": page}, **kw))
        items.extend(batch)
        if len(batch) < page:
            break
    return items[:limit]


def activity(client: GarminClient, activity_id: int, **kw) -> Optional[dict]:
    return client.get(f"/activity-service/activity/{activity_id}", **kw)


def activity_file_path(activity_id: int) -> str:
    return f"/download-service/files/activity/{activity_id}"
