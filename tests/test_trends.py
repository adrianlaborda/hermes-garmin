from datetime import date, timedelta

from hermes_garmin.trends import analyze, analyze_metric

AS_OF = date(2026, 11, 6)
NOISE = [1, -1, 2, -2, 0, 1, -1, 2, -2, 0, 1, -1, 2, -2, 0, 1, -1, 2, -2, 0, 1, -1, 2, -2, 0, 1, -1]


def series(values, end=AS_OF):
    start = end - timedelta(days=len(values) - 1)
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def test_low_hrv_is_flagged_against_baseline():
    stats, flags = analyze_metric("hrv_ms", series([50 + n for n in NOISE] + [41]), AS_OF)
    assert stats["days_with_data"] == 28
    assert stats["baseline_mean"] == 50.0
    assert stats["latest_vs_baseline_pct"] == -18.0
    assert stats["z_latest"] < -2
    assert flags[0]["kind"] == "latest_vs_baseline" and flags[0]["verdict"] == "unfavourable"
    assert flags[0]["text"].startswith("overnight HRV 18% below baseline (41 ms on 2026-11-06 vs 50 ms, z=")


def test_ordinary_noise_is_not_flagged():
    stats, flags = analyze_metric("hrv_ms", series([50 + n for n in NOISE] + [52]), AS_OF)
    assert flags == []
    assert abs(stats["z_latest"]) < 1.5


def test_direction_decides_the_verdict():
    _, flags = analyze_metric("resting_hr_bpm", series([50 + n for n in NOISE] + [58]), AS_OF)
    assert flags[0]["verdict"] == "unfavourable" and "above baseline" in flags[0]["text"]
    _, flags = analyze_metric("resting_hr_bpm", series([50 + n for n in NOISE] + [43]), AS_OF)
    assert flags[0]["verdict"] == "favourable"
    _, flags = analyze_metric("steps", series([10000 + 100 * n for n in NOISE] + [3000]), AS_OF)
    assert flags[0]["verdict"] == "neutral"


def test_sustained_change_7d_vs_28d():
    stats, flags = analyze_metric("sleep_hours", series([7.5] * 21 + [6.0] * 7), AS_OF)
    assert stats["avg_7d"] == 6.0 and stats["avg_28d"] == 7.1
    sustained = [f for f in flags if f["kind"] == "7d_vs_28d"]
    assert sustained and sustained[0]["text"].startswith("7-day sleep average 16% below the 28-day average")


def test_flat_baseline_still_flags_a_real_change():
    stats, flags = analyze_metric("sleep_score", series([80] * 10 + [60]), AS_OF)
    assert stats["baseline_sd"] == 0 and stats["z_latest"] == -12.5  # sd floored at 2% of the mean
    assert flags[0]["text"].startswith("sleep score 25% below baseline")
    _, flags = analyze_metric("sleep_score", series([80] * 10 + [81]), AS_OF)
    assert flags == []


def test_short_history_has_no_baseline():
    stats, flags = analyze_metric("hrv_ms", series([50, 52, 40]), AS_OF)
    assert "baseline_mean" not in stats and "note" in stats and flags == []


def test_stale_and_empty():
    stats, _ = analyze_metric("hrv_ms", series([50] * 10, end=AS_OF - timedelta(days=4)), AS_OF)
    assert stats["stale"] == "latest value is from 2026-11-02"
    assert analyze_metric("hrv_ms", [], AS_OF) == ({"days_with_data": 0}, [])


def test_future_points_are_ignored_and_flags_sorted():
    report = analyze({
        "steps": series([10000 + 100 * n for n in NOISE] + [3000]),
        "hrv_ms": series([50 + n for n in NOISE] + [41]) + [(AS_OF + timedelta(days=1), 99)],
    }, AS_OF, window_days=28)
    assert report["metrics"]["hrv_ms"]["latest"] == 41
    assert [f["verdict"] for f in report["flags"]] == ["unfavourable", "neutral"]
    assert report["window_days"] == 28 and "z" in report["method"]
