import json

from conftest import TZ, fixture
from hermes_garmin import summaries as s


def test_formatting():
    assert s.hmm(25500) == "7:05"
    assert s.hmm(59) == "0:01"
    assert s.hms(3725) == "1:02:05"
    assert s.hmm(None) is None and s.hmm(-1) is None
    assert s.local_time("2026-08-31 17:01:18") == "2026-08-31T17:01"
    assert s.local_time("2026-10-30T07:02:11.0") == "2026-10-30T07:02"
    assert s.local_time("2026-11-05T22:10:00.0", TZ) == "2026-11-05T23:10"
    assert s.local_time("garbage") is None and s.local_time(0) is None


def test_sleep():
    night = s.sleep(fixture("daily_sleep")[0])
    assert night["insight"] == "POSITIVE_RESTFUL_EVENING"
    assert night["date"] == "2026-11-06"
    assert night["total_sleep_hmm"] == "7:05"
    assert night["stages"]["deep"] == {"hmm": "1:25", "pct": 20}
    assert night["stages"]["awake"] == {"hmm": "0:33"}
    assert (night["score"], night["score_quality"]) == (82, "GOOD")
    # *Local epoch-ms values are the wall-clock time already
    assert night["bedtime_local"] == "2026-11-05T23:10"
    assert night["wake_local"] == "2026-11-06T07:05"
    assert night["avg_hr_bpm"] == 51 and night["lowest_spo2_pct"] == 89
    assert "naps_hmm" not in night  # zero naps dropped


def test_hrv_keeps_last_night_apart_from_the_weekly_status():
    no_night, day = (s.hrv(d) for d in fixture("hrv_daily")["hrvSummaries"])
    assert day == {"date": "2026-11-06", "last_night_avg_ms": 43, "last_night_5min_high_ms": 71,
                   "weekly_avg_ms": 52, "status": "BALANCED", "status_basis": "7-day average",
                   "balanced_range_ms": "45-60", "last_night_vs_range": "below",
                   "reading": "7-day average 52 ms (Balanced); last night 43 ms, below the usual 45-60 ms range",
                   "feedback": "HRV_BALANCED_2"}
    assert "last_night_avg_ms" not in no_night and "last_night_vs_range" not in no_night
    assert no_night["reading"] == "7-day average 53 ms (Balanced)"
    assert s.hrv({"lastNightAvg": 50, "baseline": {"balancedLow": 45, "balancedUpper": 60}})[
        "last_night_vs_range"] == "within"


def test_body_battery_uses_column_descriptors():
    day = s.body_battery(fixture("body_battery")[0], TZ)
    assert day == {"date": "2026-11-06", "charged": 54, "drained": 30, "highest": 84, "lowest": 30, "latest": 64}
    # rows out of order, the spec's four-column layout, a null level and an event list
    spec = {"bodyBatteryValueDescriptorDTOList": [
                {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
                {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryStatus"},
                {"bodyBatteryValueDescriptorIndex": 2, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"}],
            "bodyBatteryValuesArray": [[1793970000000, "MEASURED", 64], [1793923200000, "MEASURED", 30],
                                       [1793955600000, "MEASURED", None]],
            "bodyBatteryActivityEvent": [{"eventType": "SLEEP", "eventStartTimeGmt": "2026-11-05T22:10:00.0",
                                          "durationInMilliseconds": 28500000, "bodyBatteryImpact": 54,
                                          "shortFeedback": "RESTFUL_PERIOD"}]}
    day = s.body_battery(spec, TZ)
    assert (day["latest"], day["lowest"]) == (64, 30)
    assert day["events"] == [{"type": "SLEEP", "start_local": "2026-11-05T23:10", "duration_min": 475,
                              "impact": 54, "feedback": "RESTFUL_PERIOD"}]
    assert "events" not in s.body_battery(spec, TZ, events=False)


def test_stress_drops_no_data_levels():
    full, empty = (s.stress(d) for d in fixture("stress_daily"))
    assert full == {"date": "2026-11-05", "avg_stress": 34, "high_hmm": "0:30", "medium_hmm": "1:30",
                    "low_hmm": "4:00", "rest_hmm": "8:30"}
    assert empty == {"date": "2026-11-06"}


def test_steps():
    assert s.steps(fixture("steps_daily")[0]) == {"date": "2026-11-05", "steps": 11234, "goal": 10000,
                                                    "goal_pct": 112, "distance_km": 8.71}


def test_heart_rate_hourly_in_local_time():
    day = s.heart_rate(fixture("daily_heart_rate"), TZ, hourly=True)
    assert day["date"] == "2026-11-06"
    assert day["resting_bpm"] == 52 and day["resting_7d_avg_bpm"] == 53
    assert day["hourly_avg_bpm"] == {"06": 52, "07": 61}  # UTC+1, null sample ignored
    assert "hourly_avg_bpm" not in s.heart_rate(fixture("daily_heart_rate"), TZ)


def test_daily_summary():
    day = s.daily_summary(fixture("user_summary_daily"))
    assert day["body_battery"] == {"latest": 64, "at_wake": 78, "highest": 84, "lowest": 30,
                                   "charged": 54, "drained": 20}
    assert day["intensity_minutes"] == 20  # moderate + 2 x vigorous, as Garmin counts them
    assert day["steps"] == 4210 and day["resting_hr_bpm"] == 52
    assert day["last_sync_local"] == "2026-11-06T10:12"  # GMT sync + the day's own UTC offset
    assert "avg_spo2_pct" not in day  # null on watches without Pulse Ox
    assert "displayName" not in json.dumps(day) and "private-name" not in json.dumps(day)


def test_daily_summary_v2():
    flat = s.daily_summary_v2({"calendarDate": "2026-11-05", "heartRate": {"restingValue": 51},
                               "movement": {"steps": {"value": 9000}}, "stress": {"avgLevel": -2},
                               "bodyBattery": {"maxValue": 90}})
    assert flat == {"date": "2026-11-05", "resting_hr_bpm": 51, "steps": 9000, "body_battery_high": 90}


def test_weight_grams_to_kg():
    day = s.weight(fixture("weight_range")["dailyWeightSummaries"][0])
    assert day == {"date": "2026-11-04", "weight_kg": 71.5, "bmi": 22.3, "body_fat_pct": 15.2,
                   "body_water_pct": 60.1, "muscle_mass_kg": 33.1, "bone_mass_kg": 3.3, "measurements": 1}
    assert s.weight({"latestWeight": {"weight": 71.45}})["weight_kg"] == 71.5  # already kg


def test_hydration():
    assert s.hydration(fixture("hydration_daily")[0]) == {"date": "2026-11-05", "intake_ml": 2250,
                                                            "goal_ml": 2500, "sweat_loss_ml": 610}


def test_activity_labels():
    assert s.activity_label("strength_training") == "Strength training"
    assert s.activity_label("hiit") == "HIIT" and s.activity_label("") == "Activity"


def test_strength_activity():
    gym = s.activity(fixture("activities")[2])
    assert gym["type"] == "strength_training" and gym["type_label"] == "Strength training"
    assert (gym["sets"], gym["reps"], gym["exercises"]) == (12, 110, ["squat", "pull up"])
    assert gym["duration"] == "0:50:05"
    assert "distance_km" not in gym and "avg_speed_kmh" not in gym  # Garmin sends 0.0 for both


def test_activity_list_item_is_compact_and_private():
    run, ride, _gym = (s.activity(a) for a in fixture("activities"))
    assert run["pace_min_per_km"] == "5:11" and "avg_speed_kmh" not in run
    assert run["distance_km"] == 10.02 and run["duration"] == "0:52:00"
    assert run["start_local"] == "2026-11-05T07:02"
    assert run["hr_zones_hmm"]["z3"] == "0:25"
    assert ride["avg_speed_kmh"] == 26.8 and ride["avg_power_w"] == 182
    assert run["type_label"] == "Running" and ride["type_label"] == "Road biking"
    text = json.dumps([run, ride])
    for private in ("Latitude", "40.41", "Private Person", "private-name", "example.invalid"):
        assert private not in text


def test_activity_detail():
    detail = s.activity_detail(fixture("activity_detail"))
    assert detail["type"] == "running" and detail["time_zone"] == "Europe/Madrid"
    assert detail["avg_run_cadence_spm"] == 172 and detail["training_effect_label"] == "TEMPO"
    assert detail["stamina_start_end_pct"] == "100-61"
    assert detail["intensity_minutes"] == 90
    assert detail["splits"] == [{"type": "INTERVAL_ACTIVE", "count": 1, "distance_km": 10.02,
                                 "duration": "0:52:00"}]
    assert "40.41" not in json.dumps(detail)
