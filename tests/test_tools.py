import json
from datetime import timedelta

import httpx
import pytest

from conftest import API_KEY, TODAY, fixture
from hermes_garmin import tools
from hermes_garmin.client import TOKEN_PATH

ACTIVITIES = "/activitylist-service/activities/search/activities"
SLEEP = "/sleep-service/sleep/dailySleepSummariesByDate"
DAY = TODAY.isoformat()


@pytest.fixture
def routes(fake, runtime):
    fake.routes.update({
        "/usersummary-service/usersummary/daily": fixture("user_summary_daily"),
        SLEEP: fixture("daily_sleep"),
        f"/hrv-service/hrv/daily/{DAY}/{DAY}": fixture("hrv_daily"),
        ACTIVITIES: fixture("activities"),
        "/activity-service/activity/21000000002": fixture("activity_detail"),
        "/wellness-service/wellness/bodyBattery/reports/daily": fixture("body_battery"),
        "/wellness-service/wellness/dailyHeartRate": fixture("daily_heart_rate"),
        f"/usersummary-service/stats/stress/daily/2026-11-05/{DAY}": fixture("stress_daily"),
        f"/usersummary-service/stats/steps/daily/2026-11-05/{DAY}": fixture("steps_daily"),
        f"/weight-service/weight/range/2026-10-08/{DAY}": fixture("weight_range"),
        f"/usersummary-service/stats/hydration/daily/2026-10-08/{DAY}": fixture("hydration_daily"),
    })
    return fake.routes


def call(name, **args):
    handler = {n: h for n, _s, h, _e in tools.TOOLS}[name]
    return json.loads(handler(args, task_id="t1"))


def test_today_combines_every_source(routes):
    result = call("garmin_today")
    assert result["date"] == DAY
    assert result["sleep"]["total_sleep_hmm"] == "7:05"
    assert result["hrv"]["status"] == "BALANCED" and result["hrv"]["last_night_vs_range"] == "below"
    assert result["day"]["body_battery"]["at_wake"] == 78
    assert result["synced_local"] == "2026-11-06T10:12" and "inactive" in result["freshness_note"]
    assert result["latest_activity"]["name"] == "Morning Run"
    assert "unavailable" not in result


def test_today_reports_partial_failures(routes, fake):
    fake.routes[f"/hrv-service/hrv/daily/{DAY}/{DAY}"] = httpx.Response(500)
    result = call("garmin_today")
    assert "hrv" not in result and "server error" in result["unavailable"]["hrv"]
    assert result["sleep"]["score"] == 82


def test_auth_failure_is_one_clear_error(routes, fake):
    fake.routes[TOKEN_PATH] = httpx.Response(401)
    result = call("garmin_today")
    assert list(result) == ["error"] and "Create a new key" in result["error"]
    assert API_KEY not in result["error"]


def test_sleep_range_and_raw(routes, fake):
    fake.routes[SLEEP] = fixture("daily_sleep") + [{"calendarDate": "2026-11-05", "sleepTimeSeconds": 21600,
                                                    "sleepScores": {"overall": {"value": 70}}}]
    result = call("garmin_sleep", days=2)
    assert [n["date"] for n in result["nights"]] == ["2026-11-05", DAY]
    assert result["average"] == {"total_sleep_hmm": "6:32", "score": 76}
    assert "raw" not in result
    assert call("garmin_sleep", raw=True)["raw"][0]["sleepTimeSeconds"] == 25500
    params = dict(fake.calls(SLEEP)[0].url.params)
    assert params == {"startDate": "2026-11-05", "endDate": DAY}


def test_invalid_arguments_are_errors_not_exceptions(routes):
    assert "YYYY-MM-DD" in call("garmin_sleep", date="last tuesday")["error"]
    assert "positive integer" in call("garmin_activity", activity_id="abc")["error"]
    assert "at most 93" in call("garmin_stress", days=400)["error"]


def test_body_battery_single_day(routes):
    day = call("garmin_body_battery")["days"][0]
    assert (day["latest"], day["highest"], day["lowest"]) == (64, 84, 30) and "events" not in day


def test_stress_and_steps_ranges(routes):
    stress = call("garmin_stress", start_date="2026-11-05")
    assert stress["average_stress"] == 34 and stress["days"][1] == {"date": DAY}
    steps = call("garmin_steps", start_date="2026-11-05")
    assert (steps["total_steps"], steps["daily_average"], steps["days_goal_met"]) == (15444, 7722, 1)


def test_heart_rate_day_and_range(routes, fake):
    day = call("garmin_heart_rate", hourly=True)
    assert day["resting_bpm"] == 52 and day["hourly_avg_bpm"]["07"] == 61
    fake.routes["/usersummary-service/v2/dailysummaries"] = {"data": [
        {"calendarDate": "2026-11-05", "heartRate": {"restingValue": 50, "minValue": 45, "maxValue": 150},
         "movement": {"steps": {"value": 9000}}},
        {"calendarDate": DAY, "heartRate": {"restingValue": 54}}]}
    week = call("garmin_heart_rate", days=2)
    assert week["days"][0] == {"date": "2026-11-05", "resting_hr_bpm": 50, "min_hr_bpm": 45, "max_hr_bpm": 150}
    assert week["average_resting_hr_bpm"] == 52


def test_activities_filters(routes, fake):
    result = call("garmin_activities", activity_type="running", days=7, limit=2)
    assert [a["id"] for a in result["activities"]] == [21000000002, 21000000001]
    assert "Showing the newest 2" in result["note"]
    params = dict(fake.calls(ACTIVITIES)[0].url.params)
    assert params["activityType"] == "running" and params["startDate"] == "2026-10-31"


def test_activities_child_type_falls_back_to_local_filter(routes, fake):
    def search(req):
        if req.url.params.get("activityType") == "strength_training":
            return httpx.Response(400, json={"message": "Invalid activity type"})
        return fixture("activities")
    fake.routes[ACTIVITIES] = search
    result = call("garmin_activities", activity_type="strength_training", limit=5)
    assert [a["id"] for a in result["activities"]] == [21000000000]
    assert result["activities"][0]["sets"] == 12
    assert "activityType" not in dict(fake.calls(ACTIVITIES)[-1].url.params)


def test_activity_detail(routes):
    result = call("garmin_activity", activity_id=21000000002)
    assert result["training_effect_label"] == "TEMPO" and result["device"] == "GARMIN"


def test_activity_fit_downloads_into_plugin_data(routes, fake, monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_download_dir", lambda: tmp_path)
    path = "/download-service/files/activity/21000000002"
    fake.routes[path] = httpx.Response(200, content=b"PK\x03\x04data")
    result = call("garmin_activity_fit", activity_id=21000000002)
    assert result["path"] == str(tmp_path / "21000000002.zip") and result["bytes"] == 8
    assert call("garmin_activity_fit", activity_id=21000000002)["cached"] is True
    assert len(fake.calls(path)) == 1

    fake.routes["/download-service/files/activity/5"] = httpx.Response(200, content=b"<html>")
    assert "not return a ZIP" in call("garmin_activity_fit", activity_id=5)["error"]
    assert not (tmp_path / "5.zip").exists()


def test_body(routes):
    result = call("garmin_body")
    assert result["weight"][0]["weight_kg"] == 71.5
    assert result["hydration"] == [{"date": "2026-11-05", "intake_ml": 2250, "goal_ml": 2500, "sweat_loss_ml": 610}]


def test_trends_end_to_end(routes, fake):
    days = [TODAY - timedelta(days=27 - i) for i in range(28)]
    noise = [1, -1, 2, -2, 0, 1, -1] * 4
    fake.routes["/usersummary-service/v2/dailysummaries"] = lambda req: {"data": [
        {"calendarDate": d.isoformat(), "heartRate": {"restingValue": 50 + noise[i] + (8 if d == TODAY else 0)},
         "movement": {"steps": {"value": 10000 + 100 * noise[i] if d != TODAY else 900}}}
        for i, d in enumerate(days) if req.url.params["startDate"] <= d.isoformat() <= req.url.params["endDate"]]}
    fake.routes[SLEEP] = lambda req: [
        {"calendarDate": d.isoformat(), "sleepTimeSeconds": 27000 + 600 * noise[i]}
        for i, d in enumerate(days) if req.url.params["startDate"] <= d.isoformat() <= req.url.params["endDate"]]
    fake.routes.update({
        f"/hrv-service/hrv/daily/{s}/{e}": {"hrvSummaries": [
            {"calendarDate": d.isoformat(), "lastNightAvg": 50 + noise[i] - (9 if d == TODAY else 0)}
            for i, d in enumerate(days) if s <= d.isoformat() <= e]}
        for s, e in [(days[0].isoformat(), DAY)]})

    result = call("garmin_trends")
    texts = [f["text"] for f in result["flags"]]
    assert any(t.startswith("overnight HRV 20% below baseline (40 ms") for t in texts)
    assert any(t.startswith("resting heart rate 14% above baseline (57 bpm") for t in texts)
    # today's partial step count is excluded, so it cannot raise a false "steps fell" flag
    assert result["metrics"]["steps"]["latest_date"] == (TODAY - timedelta(days=1)).isoformat()
    assert not any("steps" in t for t in texts)
    assert set(result["no_data"]) == {"sleep_score", "avg_stress", "body_battery_high", "intensity_minutes"}


V2 = "/usersummary-service/v2/dailysummaries"


def _v2(end_local):
    return {"data": [{"calendarDate": DAY, "wellnessChronology": {"endTimestampLocal": end_local}}]}


def test_today_uses_the_newest_sync_when_the_daily_summary_lags(routes):
    # Right after a sync Garmin can still answer the daily summary from the 10:12 snapshot while the
    # v2 summary already ends at 15:45: report 15:45, not a stale sync six hours ago.
    routes[V2] = _v2("2026-11-06T15:45:00.0")
    result = call("garmin_today")
    assert result["synced_local"] == "2026-11-06T15:45"
    assert "freshness_note" not in result and "sync_age_hours" not in result
    assert result["day_totals_as_of"] == "2026-11-06T10:12" and "still processing" in result["day_totals_note"]
    from hermes_garmin import reports
    assert "still processing the 15:45 sync" in reports.human_text(result)


def test_today_ignores_a_quarter_hour_rounding_and_finished_days(routes):
    routes[V2] = _v2("2026-11-06T10:15:00.0")
    result = call("garmin_today")
    assert result["synced_local"] == "2026-11-06T10:12" and "day_totals_note" not in result
    assert "5 h ago" in result["freshness_note"]
    from hermes_garmin.reports import latest_sync
    assert latest_sync(TODAY - timedelta(days=1), "2026-11-05T21:00", "2026-11-06T00:00:00.0") == "2026-11-05T21:00"


def test_sleep_and_hrv_detail_for_one_night(routes, fake):
    fake.routes["/sleep-service/sleep/dailySleep"] = fixture("daily_sleep")[0]
    fake.routes[f"/hrv-service/hrv/{DAY}"] = fixture("hrv_night")
    night = call("garmin_sleep", detail=True)
    assert night["date"] == DAY and night["score"] == 82 and "below_good" not in night["detail"]
    assert dict(fake.calls("/sleep-service/sleep/dailySleep")[0].url.params) == {"date": DAY}
    ranged = call("garmin_sleep", detail=True, days=3)
    assert DAY in ranged["detail_note"]
    hrv = call("garmin_hrv", detail=True)
    assert hrv["last_night_avg_ms"] == 50 and hrv["overnight"]["second_half_avg_ms"] == 57
    fake.routes["/sleep-service/sleep/dailySleep"] = httpx.Response(204)
    assert "No sleep recorded" in call("garmin_sleep", detail=True, date="yesterday")["note"]
    fake.routes[f"/hrv-service/hrv/{TODAY - timedelta(days=1)}"] = {"userProfilePk": 42, "hrvReadings": []}
    assert "No overnight HRV" in call("garmin_hrv", detail=True, date="yesterday")["note"]
