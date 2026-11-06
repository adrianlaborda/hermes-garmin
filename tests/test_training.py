"""Heart-rate zones, training intensity distribution and fitness age (hermes_garmin.training)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from conftest import TODAY, fixture
from hermes_garmin import endpoints, tools, training

ACTIVITIES = "/activitylist-service/activities/search/activities"
ZONES = "/biometric-service/heartRateZones/v2"
DAILY_V2 = "/usersummary-service/v2/dailysummaries"
FITNESS_AGE = f"/fitnessage-service/fitnessage/v2/{TODAY.isoformat()}"


def act(day, z=(0, 0, 0, 0, 0), *, type_key="running", duration=None, activity_id=1):
    dto = {"activityId": activity_id, "startTimeLocal": f"{day.isoformat()} 07:00:00",
           "activityType": {"typeKey": type_key}}
    if z is not None:
        dto.update({f"hrTimeInZone_{i + 1}": float(s) for i, s in enumerate(z)})
    if duration is not None:
        dto["duration"] = float(duration)
    return dto


def daily(day, moderate, vigorous, goal=150):
    return {"calendarDate": day.isoformat(),
            "intensityMinutes": {"moderate": moderate, "vigorous": vigorous, "goal": goal}}


def window_days(n=7):
    return [TODAY - timedelta(days=i) for i in range(n)]


def baseline_days(n=7, base=28):
    return [TODAY - timedelta(days=n + i) for i in range(base)]


# -- zone definitions -------------------------------------------------------------------------------

def test_zone_bounds_from_floors_and_max():
    (zones,) = training.zone_sets(fixture("heart_rate_zones"))
    assert zones["sport"] == "DEFAULT" and zones["method"] == "LACTATE_THRESHOLD"
    assert [training.bpm_range(z) for z in zones["zones"]] == [
        "100-119", "120-139", "140-154", "155-169", "170-188"]
    assert "resting_hr_bpm" not in zones  # the live API sends null with lactate-threshold zones


def test_unusable_zone_sets_are_dropped_and_sport_picks_its_set():
    default = fixture("heart_rate_zones")[0]
    running = {**default, "sport": "RUNNING", "zone1Floor": 110}
    broken = {**default, "sport": "CYCLING", "zone3Floor": 100}  # not ascending
    missing = {"sport": "SWIMMING", "zone1Floor": 90}
    sets = training.zone_sets([default, running, broken, missing, "junk"])
    assert [s["sport"] for s in sets] == ["DEFAULT", "RUNNING"]
    assert training.zones_for(sets, "trail_running")["sport"] == "RUNNING"
    assert training.zones_for(sets, "road_biking")["sport"] == "DEFAULT"
    assert training.zones_for([], "running") is None
    without_max = training.zone_sets([{**default, "maxHeartRateUsed": None}])[0]
    assert training.bpm_range(without_max["zones"][4]) == "170+"


# -- one activity -----------------------------------------------------------------------------------

def test_activity_zones_minutes_shares_and_bounds():
    run = fixture("activities")[0]  # 120/600/1500/840/60 s over a 3,120 s run
    out = training.activity_zones(run, training.zone_sets(fixture("heart_rate_zones"))[0])
    assert [(z["zone"], z["minutes"], z["pct"], z["bpm"]) for z in out["zones"]] == [
        (1, 2.0, 4, "100-119"), (2, 10.0, 19, "120-139"), (3, 25.0, 48, "140-154"),
        (4, 14.0, 27, "155-169"), (5, 1.0, 2, "170-188")]
    assert out["in_zones_min"] == 52.0 and out["easy_pct_z1_z2"] == 23 and out["hard_pct_z4_z5"] == 29
    assert "outside_zones_min" not in out
    assert out["zone_basis"]["method"] == "LACTATE_THRESHOLD"


def test_activity_zones_time_outside_zones_and_no_hr():
    gym = act(TODAY, (1200, 900, 300, 0, 0), duration=3000, type_key="strength_training")
    out = training.activity_zones(gym, None)
    assert out["outside_zones_min"] == 10.0 and "bpm" not in out["zones"][0]
    assert training.activity_zones(act(TODAY, None), None) is None


# -- training load ----------------------------------------------------------------------------------

def load_history():
    """Two sessions a week for the 4 baseline weeks (15 min/week in zones 4-5), then two harder ones."""
    acts = [act(d, (0, 2400, 600, 450, 0), activity_id=100 + i)
            for i, d in enumerate(baseline_days()[::14] + baseline_days()[3::14] + baseline_days()[7::14]
                                  + baseline_days()[10::14])]
    assert len(acts) == 8
    acts += [act(TODAY - timedelta(days=1), (0, 2400, 600, 900, 45), activity_id=1),
             act(TODAY - timedelta(days=4), (0, 2400, 600, 900, 45), type_key="road_biking", activity_id=2),
             act(TODAY - timedelta(days=2), None, type_key="strength_training", activity_id=3),
             act(TODAY - timedelta(days=60), (0, 0, 0, 9000, 0), activity_id=4)]  # before the baseline
    dailies = ([daily(d, 20, 10) for d in window_days()] + [daily(d, 10, 5) for d in baseline_days()])
    return acts, dailies


def test_training_load_hard_time_against_four_week_average():
    acts, dailies = load_history()
    out = training.analyze_load(acts, dailies, fixture("heart_rate_zones"), TODAY)
    assert out["range"] == f"{TODAY - timedelta(days=6)}..{TODAY}"
    assert out["baseline_range"] == f"{TODAY - timedelta(days=34)}..{TODAY - timedelta(days=7)}"
    window = out["window"]
    assert window["activities"] == 3 and window["with_hr_zones"] == 2
    assert window["zone_minutes"] == {"z1": 0.0, "z2": 80.0, "z3": 20.0, "z4": 30.0, "z5": 1.5}
    assert (window["easy_pct_z1_z2"], window["middle_pct_z3"], window["hard_pct_z4_z5"]) == (61, 15, 24)
    assert out["baseline_weekly_avg"]["z4_z5_min"] == 15.0
    assert out["baseline_weekly_avg"]["in_zones_min"] == 115.0
    assert out["vs_baseline"] == {"in_zones_ratio": 1.14, "z4_z5_ratio": 2.1}
    assert out["zones"]["bpm"]["z4"] == "155-169"
    assert [t["type_label"] for t in out["by_type"]] == ["Running", "Road biking"]
    assert "1 activity(ies) in the window had no heart-rate data" in out["note"]
    texts = [f["text"] for f in out["flags"]]
    assert texts == ["Zone 4-5 time 2.1x your 4-week weekly average (32 min this week vs 15 min/week)",
                     "280 of 150 weekly intensity minutes (187%) in the last 7 days"]
    assert out["flags"][0]["caution"] is True
    im = out["intensity_minutes"]
    assert (im["moderate"], im["vigorous"], im["total"], im["weekly_goal"], im["baseline_weekly_avg"]) == (
        140, 70, 280, 150, 140)


def test_training_load_distribution_flags():
    easy = [act(d, (600, 3000, 120, 60, 0), activity_id=i) for i, d in enumerate(window_days()[:3])]
    out = training.analyze_load(easy, [], [], TODAY)
    assert [f["text"] for f in out["flags"]] == ["95% of time in zones 1-2: almost all easy"]
    assert "zones" not in out and "vs_baseline" not in out and "intensity_minutes" not in out

    middle = [act(TODAY, (0, 1200, 1800, 600, 0))]
    hard = [act(TODAY, (0, 1200, 600, 900, 300))]
    assert [f["text"] for f in training.analyze_load(middle, [], [], TODAY)["flags"]] == [
        "50% of time in zone 3, the moderate middle"]
    assert [f["text"] for f in training.analyze_load(hard, [], [], TODAY)["flags"]] == [
        "40% of time in zones 4-5"]
    short = [act(TODAY, (0, 1500, 0, 0, 0))]  # 25 min: too little time to judge a distribution
    assert training.analyze_load(short, [], [], TODAY)["flags"] == []


def test_training_load_new_hard_work_and_drops():
    base = [act(d, (0, 3600, 0, 60, 0), activity_id=i) for i, d in enumerate(baseline_days()[::4])]
    new_hard = base + [act(TODAY, (0, 1800, 0, 1500, 0), activity_id=99)]
    flag = training.analyze_load(new_hard, [], [], TODAY)["flags"][0]
    assert flag["text"] == "25 min in zones 4-5 this week, up from almost none (2 min/week over the previous 4 weeks)"

    steady = [act(d, (0, 3000, 600, 900, 0), activity_id=i) for i, d in enumerate(baseline_days()[::3])]
    light = steady + [act(TODAY, (0, 1800, 0, 120, 0), activity_id=99)]
    texts = [f["text"] for f in training.analyze_load(light, [], [], TODAY)["flags"]]
    assert texts[0] == "Zone 4-5 time 0.1x your 4-week weekly average (2 min this week vs 38 min/week)"
    assert texts[1] == "Training time in heart-rate zones 0.2x your 4-week weekly average (32 min this week vs 3 h 08 min/week)"


def test_training_load_longer_window_is_scaled_to_a_week():
    acts = [act(d, (0, 3600, 0, 600, 0), activity_id=i) for i, d in enumerate(window_days(14)[::2])]
    out = training.analyze_load(acts, [daily(d, 30, 0) for d in window_days(14)], [], TODAY, 14)
    assert out["window"]["z4_z5_min"] == 70.0
    assert out["window_per_week"] == {"in_zones_min": 245.0, "z4_z5_min": 35.0}
    assert out["intensity_minutes"]["per_week"] == 210
    assert not any(f["kind"] == "intensity_minutes_goal" for f in out["flags"])


def test_training_load_without_activities():
    out = training.analyze_load([], [], [], TODAY)
    assert out["flags"] == [] and "No activity with heart-rate zones" in out["note"]
    assert out["window"] == {"activities": 0, "with_hr_zones": 0}


# -- fitness age ------------------------------------------------------------------------------------

def test_fitness_age_components_targets_and_priorities():
    out = training.fitness_age(fixture("fitness_age"))
    assert (out["fitness_age"], out["chronological_age"], out["years_vs_age"]) == (35.4, 40, -4.6)
    assert out["reading"] == "4.6 years younger than your age"
    assert out["change_since_previous"] == -0.5 and out["achievable_fitness_age"] == 31.2
    assert out["last_updated"] == "2026-11-06"
    assert [c["name"] for c in out["components"]] == [
        "bmi", "vigorous_days_per_week", "vigorous_minutes_per_week", "resting_hr"]
    bmi, days, _minutes, rhr = out["components"]
    assert bmi["on_target"] is False and bmi["fitness_age_at_target"] == 33.0
    # Garmin's potential age above the current fitness age is not presented as a gain.
    assert "fitness_age_at_target" not in days
    assert rhr == {"name": "resting_hr", "label": "Resting heart rate", "value": 52, "unit": "bpm",
                   "better": "lower"}
    assert out["to_improve"] == [
        "BMI: 25.8 -> target 23 (fitness age about 33 at target)",
        "Days per week with vigorous activity: 2.4 days/week -> target 4 days/week",
        "Vigorous intensity minutes per week: 95.5 min/week -> target 150 min/week",
    ]
    assert "stale_note" not in out


def test_fitness_age_stale_invalid_and_missing():
    dto = fixture("fitness_age")
    dto["components"]["bmi"]["stale"] = True
    dto["fitnessAge"] = 41.0
    out = training.fitness_age(dto)
    assert out["reading"] == "1.0 years older than your age" and "BMI" in out["stale_note"]
    invalid = training.fitness_age({"invalidReason": "USER_EMPTY_AGE"})
    assert invalid["invalid_reason"] == "USER_EMPTY_AGE" and "birth date" in invalid["note"]
    assert "no fitness age" in training.fitness_age(None)["note"]


# -- endpoints and tools ----------------------------------------------------------------------------

def test_fitness_age_endpoint_accepts_object_or_array(fake, make_client):
    client = make_client()
    fake.routes[FITNESS_AGE] = fixture("fitness_age")
    assert endpoints.fitness_age(client, TODAY)["fitnessAge"] == 35.4
    fake.routes[FITNESS_AGE] = [fixture("fitness_age")]
    assert endpoints.fitness_age(client, TODAY, ttl=0)["fitnessAge"] == 35.4
    fake.routes[FITNESS_AGE] = []
    assert endpoints.fitness_age(client, TODAY, ttl=0) is None


def test_zones_are_cached_for_twelve_hours(fake, make_client, clock):
    client = make_client()
    fake.routes[ZONES] = fixture("heart_rate_zones")
    endpoints.heart_rate_zones(client)
    clock.now += 11 * 3600
    endpoints.heart_rate_zones(client)
    assert len(fake.calls(ZONES)) == 1
    clock.now += 2 * 3600
    endpoints.heart_rate_zones(client)
    assert len(fake.calls(ZONES)) == 2


@pytest.fixture
def routes(fake, runtime):
    acts, dailies = load_history()

    def in_range(req):
        start, end = req.url.params.get("startDate"), req.url.params.get("endDate")
        return [a for a in acts if (not start or start <= a["startTimeLocal"][:10] <= end)]

    fake.routes.update({
        ACTIVITIES: in_range,
        DAILY_V2: {"data": dailies},
        ZONES: fixture("heart_rate_zones"),
        FITNESS_AGE: fixture("fitness_age"),
        "/activity-service/activity/1": {
            "activityId": 1, "activityTypeDTO": {"typeKey": "running"},
            "summaryDTO": {"startTimeLocal": f"{TODAY - timedelta(days=1)}T07:00:00.0", "duration": 4000.0}},
    })
    return fake.routes


def call(name, **args):
    handler = {n: h for n, _s, h, _e in tools.TOOLS}[name]
    return json.loads(handler(args, task_id="t1"))


def test_training_load_tool(routes, fake):
    out = call("garmin_training_load")
    assert out["vs_baseline"]["z4_z5_ratio"] == 2.1
    assert out["flags"][0]["text"].startswith("Zone 4-5 time 2.1x")
    params = dict(fake.calls(ACTIVITIES)[0].url.params)
    assert params["startDate"] == str(TODAY - timedelta(days=34)) and params["endDate"] == str(TODAY)
    assert "today counts up to the watch's last sync" in out["intensity_minutes"]["note"]
    earlier = call("garmin_training_load", days=3, as_of=str(TODAY - timedelta(days=3)))
    assert earlier["range"] == f"{TODAY - timedelta(days=5)}..{TODAY - timedelta(days=3)}"
    assert "today counts" not in earlier["intensity_minutes"]["note"]


def test_activity_tool_adds_time_in_zone(routes, fake):
    out = call("garmin_activity", activity_id=1)
    assert [z["bpm"] for z in out["hr_zones"]["zones"]][3] == "155-169"
    assert out["hr_zones"]["zones"][3]["minutes"] == 15.0
    params = dict(fake.calls(ACTIVITIES)[-1].url.params)
    assert params["startDate"] == str(TODAY - timedelta(days=2)) and params["endDate"] == str(TODAY)
    fake.routes["/activity-service/activity/3"] = {
        "activityId": 3, "summaryDTO": {"startTimeLocal": f"{TODAY - timedelta(days=2)}T07:00:00.0"}}
    assert "no heart rate" in call("garmin_activity", activity_id=3)["hr_zones_note"]


def test_fitness_age_tool(routes):
    out = call("garmin_fitness_age")
    assert out["as_of"] == TODAY.isoformat() and out["fitness_age"] == 35.4
    assert "raw" not in out and call("garmin_fitness_age", raw=True)["raw"]["chronologicalAge"] == 40
