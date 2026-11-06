"""The Desktop panel payloads (hermes_garmin.panel) and their routes (dashboard/plugin_api.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import timedelta

import httpx
import pytest

from conftest import API_KEY, ROOT, TODAY, TZ, fixture, make_jwt
from hermes_garmin import panel
from hermes_garmin.client import TOKEN_PATH

DAY = TODAY.isoformat()
DAYS = [TODAY - timedelta(days=27 - i) for i in range(28)]
NOISE = [1, -1, 2, -2, 0, 1, -1] * 4
START = DAYS[0].isoformat()


def install_history(fake) -> None:
    """28 days of steady data, then a night with low HRV and a higher resting heart rate."""
    def in_range(req, day):
        return req.url.params["startDate"] <= day.isoformat() <= req.url.params["endDate"]

    def daily_v2(req):
        return {"data": [
            {"calendarDate": d.isoformat(),
             "heartRate": {"restingValue": 58 if d == TODAY else 50 + NOISE[i]},
             "movement": {"steps": {"value": 9000 + 150 * NOISE[i]}},
             "stress": {"avgLevel": 30 + NOISE[i]},
             "bodyBattery": {"maxValue": 80 + NOISE[i], "minValue": 25}}
            for i, d in enumerate(DAYS) if in_range(req, d)]}

    def sleep(req):
        rows = [{"calendarDate": d.isoformat(), "sleepTimeSeconds": 26000 + 600 * NOISE[i],
                 "sleepScores": {"overall": {"value": 80 + NOISE[i], "qualifierKey": "GOOD"}}}
                for i, d in enumerate(DAYS) if in_range(req, d) and d != TODAY]
        return rows + [r for r in fixture("daily_sleep") if in_range(req, TODAY)]

    def hrv(days):
        return {"hrvSummaries": [
            {"calendarDate": d.isoformat(), "lastNightAvg": 41 if d == TODAY else 50 + NOISE[i],
             "status": "UNBALANCED" if d == TODAY else "BALANCED",
             "baseline": {"balancedLow": 45, "balancedUpper": 60}}
            for i, d in enumerate(DAYS) if d in days]}

    fake.routes.update({
        "/usersummary-service/usersummary/daily": fixture("user_summary_daily"),
        "/usersummary-service/v2/dailysummaries": daily_v2,
        "/sleep-service/sleep/dailySleepSummariesByDate": sleep,
        f"/hrv-service/hrv/daily/{DAY}/{DAY}": hrv({TODAY}),
        **{f"/hrv-service/hrv/daily/{TODAY - timedelta(days=n - 1)}/{DAY}":
           hrv({TODAY - timedelta(days=k) for k in range(n)}) for n in range(2, 32)},
        "/activitylist-service/activities/search/activities": fixture("activities"),
    })


@pytest.fixture
def history(fake, runtime):
    install_history(fake)
    return fake


def by_id(cards):
    return {c["id"]: c for c in cards}


# -- panel payloads -------------------------------------------------------------------------------

def test_overview_builds_cards_signals_and_activity(history, runtime):
    out = panel.overview(runtime)
    assert out["state"] == "ok" and out["date"] == DAY and out["window_days"] == 28
    cards = by_id(out["cards"])
    assert list(cards) == ["sleep", "hrv", "body_battery", "stress", "resting_hr", "steps"]

    hrv = cards["hrv"]
    assert hrv["value"] == 41 and hrv["unit"] == "ms" and hrv["tone"] == "bad"
    assert hrv["baseline"] == pytest.approx(50.1, abs=0.2) and hrv["delta_pct"] < -15
    assert hrv["badge"] == "−18%" and "28-day baseline" in hrv["badge_label"]
    assert hrv["detail"] == "Below your 45–60 ms range · Unbalanced" and hrv["partial"] is False
    assert len(hrv["series"]) == 28 and hrv["series"][-1] == [DAY, 41.0]

    steps = cards["steps"]
    assert steps["display"] == "4,210" and steps["goal_pct"] == 53 and steps["badge"] == "53%"
    assert steps["tone"] == "neutral"
    # today's partial count is not in the trend series, so the sparkline gets it appended once
    assert steps["series"][-1] == [DAY, 4210.0] and len(steps["series"]) == 28

    battery = cards["body_battery"]
    assert battery["value"] == 64 and battery["detail"] == "↑84 ↓30 · wake 78"
    # running totals of an unfinished day get no baseline badge (the Signals use complete days)
    for partial in (battery, cards["stress"], steps):
        assert partial["partial"] is True and partial["delta_pct"] is None
    assert "badge" not in battery and "badge" not in cards["stress"]
    # the fixture's watch last synced at 10:12, hours before "now": the cards say so
    assert cards["stress"]["detail"] == "as of 10:12 sync · max 88"
    assert steps["detail"] == "as of 10:12 sync · goal 8,000"
    assert out["synced_local"] == "2026-11-06T10:12" and out["sync_age_hours"] == 5
    assert "do not mean the user was inactive" in out["freshness_note"]
    assert cards["resting_hr"]["better"] == "lower" and cards["stress"]["value"] == 31
    assert cards["sleep"]["metric"] == "sleep_score" and cards["sleep"]["detail"] == "7:05 h · Good"

    headlines = [s["headline"] for s in out["signals"]]
    assert "Overnight HRV 18% below your baseline" in headlines
    hrv_signal = next(s for s in out["signals"] if s["metric"] == "hrv_ms")
    assert hrv_signal["detail"] == "41 ms today vs 50 ms usual"
    assert any(h.startswith("Resting heart rate") and "above your baseline" in h for h in headlines)
    assert all(s["tone"] == "bad" for s in out["signals"][:2])

    activity = out["last_activity"]
    assert activity["name"] == "Morning Run" and activity["type_label"] == "Running"
    assert activity["distance_km"] == 10.02 and "ownerFullName" not in json.dumps(out)
    assert out["status"]["key_configured"] is True and "unavailable" not in out


def test_payloads_never_contain_the_key_or_token(history, runtime):
    token = make_jwt(history.clock.now + 86_400)
    for payload in (panel.overview(runtime), panel.status(runtime, check=True), panel.today(runtime),
                    panel.trend(runtime), panel.series(runtime, "hrv")):
        text = json.dumps(payload)
        assert API_KEY not in text and token not in text


def test_not_configured_is_an_onboarding_state(fake, runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_key_provider", lambda: None)
    for payload in (panel.overview(runtime), panel.today(runtime), panel.trend(runtime), panel.status(runtime)):
        assert payload["state"] == "not_configured"
        assert payload["key_page"].startswith("https://connect.garmin.com/")
    assert fake.requests == []


def test_rejected_key_is_an_auth_state(history, runtime):
    history.routes[TOKEN_PATH] = httpx.Response(401)
    out = panel.overview(runtime)
    assert out["state"] == "auth_error" and "Create a new key" in out["message"]
    assert API_KEY not in out["message"]
    status = panel.status(runtime, check=True)
    assert status["state"] == "auth_error" and status["token_ok"] is False


def test_rate_limit_and_server_errors_are_states(history, runtime):
    history.routes[TOKEN_PATH] = httpx.Response(429)
    assert panel.today(runtime)["state"] == "rate_limited"
    history.routes[TOKEN_PATH] = httpx.Response(503)
    assert panel.trend(runtime)["state"] == "error"


def test_partial_failures_keep_the_rest(history, runtime):
    history.routes[f"/hrv-service/hrv/daily/{DAY}/{DAY}"] = httpx.Response(500)
    out = panel.overview(runtime)
    assert out["state"] == "ok" and "hrv" in out["unavailable"]
    assert by_id(out["cards"])["hrv"]["value"] is None and by_id(out["cards"])["sleep"]["value"] == 82


def test_status_reports_key_expiry_and_token(history, runtime, monkeypatch):
    key = make_jwt(history.clock.now + 5 * 86_400)
    monkeypatch.setattr(runtime, "_key_provider", lambda: key)
    out = panel.status(runtime, check=True)
    assert out["state"] == "ok" and out["token_ok"] is True and out["region"] == "global"
    assert out["key_expires_at"] and "token_valid_until" in out
    assert key not in json.dumps(out)
    assert "token_ok" not in panel.status(runtime)  # no network without check


def test_trends_include_signals_and_series(history, runtime):
    out = panel.trend(runtime, "28")
    assert out["state"] == "ok" and out["signals"] and out["flags"]
    assert len(out["series"]["hrv_ms"]) == 28 and out["series"]["hrv_ms"][-1] == [DAY, 41.0]
    assert "series" not in panel.trend(runtime, 28, include_series=False)
    assert panel.trend(runtime, "999")["window_days"] == 120


def test_series_accepts_aliases_and_rejects_unknown_metrics(history, runtime):
    out = panel.series(runtime, "hrv", 14)
    assert out["metric"] == "hrv_ms" and out["unit"] == "ms" and out["days"] == 14
    assert len(out["points"]) == 14
    assert panel.series(runtime, "rhr")["metric"] == "resting_hr_bpm"
    bad = panel.series(runtime, "vo2")
    assert bad["state"] == "error" and "unknown metric" in bad["message"]


def test_fresh_overview_skips_the_cache(history, runtime):
    panel.overview(runtime)
    before = len(history.requests)
    panel.overview(runtime)
    assert len(history.requests) == before  # served from the client's cache
    panel.overview(runtime, fresh=True)
    assert len(history.requests) > before


def test_steps_goal_met_is_good():
    cards = panel.cards({"day": {"steps": 12000, "step_goal": 10000}}, {}, TODAY)
    steps = by_id(cards)["steps"]
    assert steps["goal_pct"] == 120 and steps["tone"] == "good"


def test_cards_without_any_data_render_placeholders():
    cards = panel.cards({}, {}, TODAY)
    assert all(c["value"] is None and c["display"] is None and c["series"] == [] for c in cards)
    assert all(c["tone"] == "neutral" and "badge" not in c for c in cards)


def test_hermes_plugin_setting_reads_the_profile_config(monkeypatch):
    from hermes_garmin import runtime as runtime_mod
    config = {"plugins": {"entries": {"garmin-connect": {"settings": {"region": "china"}}}}}
    module = types.ModuleType("hermes_cli.config")
    module.load_config_readonly = lambda: config
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    assert runtime_mod.hermes_plugin_setting("region", "global") == "china"
    assert runtime_mod.hermes_plugin_setting("timezone", "") == ""
    config["plugins"] = None
    assert runtime_mod.hermes_plugin_setting("region", "global") == "global"


# -- dashboard/plugin_api.py ----------------------------------------------------------------------

class _StubRouter:
    """Enough of ``fastapi.APIRouter`` to collect the routes when FastAPI is not installed."""

    def __init__(self):
        self.routes = {}

    def get(self, path, **_kw):
        def decorate(fn):
            self.routes[path] = fn
            return fn
        return decorate


def _load_api(monkeypatch):
    try:
        import fastapi  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fastapi")
        stub.APIRouter = _StubRouter
        monkeypatch.setitem(sys.modules, "fastapi", stub)
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_garmin_connect_test",
                                                  ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api(monkeypatch, fake, clock, sleeps):
    install_history(fake)
    module = _load_api(monkeypatch)
    client_mod = importlib.import_module(module._PACKAGE_NAME + ".client")
    client = client_mod.GarminClient(lambda: API_KEY, transport=httpx.MockTransport(fake.handler), clock=clock,
                                     sleep=sleeps.append)
    rt = module.RUNTIME
    monkeypatch.setattr(rt, "_clients", {client.base_url: client})
    monkeypatch.setattr(rt, "_key_provider", lambda: API_KEY)
    monkeypatch.setattr(rt, "today", lambda: TODAY)
    monkeypatch.setattr(rt, "tz", lambda: TZ)
    return module


def test_plugin_api_routes(api, monkeypatch):
    assert api.overview()["state"] == "ok"
    assert api.overview(days="abc")["window_days"] == 28
    assert api.today()["sleep"]["score"] == 82
    assert api.trends(days="21", series="false")["window_days"] == 21
    assert api.series(metric="stress", days="10")["metric"] == "avg_stress"
    assert api.status(check="true")["token_ok"] is True
    monkeypatch.setattr(api.RUNTIME, "_key_provider", lambda: None)
    assert api.overview()["state"] == "not_configured"
    assert api.status()["key_configured"] is False


def test_plugin_api_package_is_private(api):
    # Loaded by path under its own name: never shadows or rebinds the agent half's modules.
    assert api._PACKAGE_NAME.startswith("_garmin_connect_panel_")
    assert api.panel.__name__ == api._PACKAGE_NAME + ".panel"


def test_plugin_api_over_http(api):
    testclient = pytest.importorskip("fastapi.testclient")
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/garmin-connect")
    http = testclient.TestClient(app)
    for path in ("/overview", "/today", "/trends?days=28", "/series?metric=hrv&days=28", "/status?check=true"):
        response = http.get("/api/plugins/garmin-connect" + path)
        assert response.status_code == 200, path
        assert response.json()["state"] == "ok", path
        assert API_KEY not in response.text
