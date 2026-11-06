import contextvars
import threading
import time

import httpx
import pytest

from conftest import TODAY, fixture
from hermes_garmin.client import TOKEN_PATH
from hermes_garmin.context import DailyContext

DAY = TODAY.isoformat()


@pytest.fixture
def enabled(fake, runtime, settings):
    settings["inject_daily_context"] = True
    fake.routes.update({
        "/usersummary-service/usersummary/daily": fixture("user_summary_daily"),
        "/sleep-service/sleep/dailySleepSummariesByDate": fixture("daily_sleep"),
        f"/hrv-service/hrv/daily/{DAY}/{DAY}": fixture("hrv_daily"),
        "/activitylist-service/activities/search/activities": fixture("activities"),
    })
    return runtime


def test_off_by_default(fake, runtime):
    assert DailyContext(runtime)(session_id="s1") is None
    assert fake.requests == []


def test_injects_once_per_session_and_day(enabled, fake):
    hook = DailyContext(enabled)
    result = hook(session_id="s1", user_message="plan my week", is_first_turn=True)
    line = result["context"]
    assert len(line) < 600
    for expected in ("slept 7:05 h (score 82)",
                     "HRV 7-day avg 52 ms (Balanced); last night 43 ms, below your usual 45–60 ms",
                     "Body Battery 64, 78 at wake", "resting HR 52 bpm", "4,210 steps of 8,000",
                     "watch last synced 10:12 (today's totals stop there)",
                     "last activity Running 10.02 km on 2026-11-05", "not medical advice"):
        assert expected in line
    assert hook(session_id="s1") is None

    calls = len(fake.requests)
    assert hook(session_id="s2")["context"] == line  # new session, same day: cached line
    assert len(fake.requests) == calls


def test_skips_subagents(enabled, fake):
    assert DailyContext(enabled)(session_id="child", parent_session_id="s1") is None
    assert fake.requests == []


def test_errors_are_silent_and_backed_off(enabled, fake):
    fake.routes[TOKEN_PATH] = httpx.Response(500)
    hook = DailyContext(enabled)
    assert hook(session_id="s1") is None
    calls = len(fake.requests)
    assert hook(session_id="s2") is None
    assert len(fake.requests) == calls  # no new attempt inside the backoff window


def test_unexpected_exceptions_never_escape(enabled, monkeypatch):
    monkeypatch.setattr(enabled, "client", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert DailyContext(enabled)(session_id="s1") is None


def test_respects_the_time_budget(enabled, fake):
    release = threading.Event()

    def slow(request):
        release.wait(5)
        return fixture("user_summary_daily")

    fake.routes["/usersummary-service/usersummary/daily"] = slow
    hook = DailyContext(enabled, budget=0.2)
    started = time.monotonic()
    assert hook(session_id="s1") is None
    assert time.monotonic() - started < 1.0
    release.set()
    hook._inflight.join(2)
    # the late result warmed the cache for the next session
    assert "slept 7:05" in hook(session_id="s2")["context"]


def test_no_retries_inside_the_hook(enabled, fake, sleeps):
    fake.routes["/usersummary-service/usersummary/daily"] = httpx.Response(503)
    hook = DailyContext(enabled)
    result = hook(session_id="s1")
    assert sleeps == []
    assert "Body Battery" not in result["context"] and "slept 7:05" in result["context"]


def test_worker_threads_see_the_callers_secret_scope(enabled, monkeypatch):
    scope = contextvars.ContextVar("secret_scope", default=None)
    monkeypatch.setattr(enabled, "_key_provider", lambda: scope.get())
    for client in enabled._clients.values():
        monkeypatch.setattr(client, "_key_provider", lambda: scope.get())
    scope.set("profile-key")
    assert "slept 7:05" in DailyContext(enabled)(session_id="s1")["context"]
