# Changelog

All notable changes to the `garmin-connect` plugin are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-06

First release.

### Added

- Garmin Connect through Garmin's official Personal API, with a Personal API key read through Hermes' secret
  route. The key is exchanged for an access token that stays in memory and renews itself before it expires.
- The key can be entered without a terminal: Hermes Desktop shows a masked **Garmin Personal API key** field in
  **Settings > Plugins > garmin-connect** (manifest `config_schema` secret, stored in the profile's `.env` as
  `GARMIN_PERSONAL_API_KEY`). No restart is needed after saving it. The installer also prompts for it.
- Read-only tools: `garmin_today`, `garmin_sleep`, `garmin_hrv`, `garmin_body_battery`, `garmin_stress`,
  `garmin_steps`, `garmin_heart_rate`, `garmin_activities`, `garmin_activity`, `garmin_activity_fit`,
  `garmin_body`, `garmin_trends`, `garmin_training_load` and `garmin_fitness_age`. They are hidden until the
  key is set.
- `garmin_training_load`: minutes in heart-rate zones 1-5 over the last N days (7 by default) with each zone's
  bpm range, easy/moderate/hard shares, time per activity type and intensity minutes against the weekly goal,
  compared with the weekly average of the 4 weeks before, and deterministic flags ("Zone 4-5 time 2.1x your
  4-week weekly average", "92% of time in zones 1-2"). Heart-rate zones are cached for 12 hours.
- `garmin_activity` adds time in each heart-rate zone (minutes, % and bpm range) from the activity list, since
  the detail endpoint does not carry it.
- `garmin_fitness_age`: Garmin's fitness age against the user's age, previous and achievable fitness age, and
  each input (vigorous days and minutes per week, resting heart rate, BMI or body fat) against Garmin's target,
  ordered by priority. Accepts the single object the live API returns as well as the documented array.
- `garmin_sleep` and `garmin_hrv` take `detail: true` for one night. Sleep: each sleep-score component with its
  value, Garmin's rating and optimal range, the components rated below GOOD, the respiration range, and sleep
  need and naps when present (Garmin's Personal API has no stage timeline). HRV: 30-minute averages of the
  5-minute overnight readings, first vs second half of the night, and the lowest and highest window.
- `garmin_trends`: baselines, z-scores, 7-day vs 28-day changes and plain-language flags for HRV, resting heart
  rate, sleep, sleep score, stress, Body Battery, steps and intensity minutes. Today's running totals are compared
  up to yesterday.
- `garmin_today`, `/garmin today`, the daily context line and the panel say when the watch last synced
  (`synced_local`). When that is more than 2 hours ago, the tool adds a `freshness_note` telling the model that
  low steps or stress mean an old sync, not inactivity. The sync time is the newest of the daily summary's
  last sync and the end of the day's wellness data, so a daily summary Garmin is still processing right after a
  sync does not report the previous sync; the day's totals then carry `day_totals_note`.
- HRV summaries include `last_night_vs_range` (`below`, `within`, `above`), `status_basis` and a `reading`
  sentence, since Garmin's status rates the 7-day average, not last night.
- Activities carry a readable `type_label` ("Strength training"); strength workouts add `sets`, `reps` and
  `exercises`; runs add `avg_run_cadence_spm`. Sleep adds Garmin's `insight`.
- `garmin_activities` accepts `strength_training` and other types Garmin's filter rejects with HTTP 400: the
  plugin then filters the newest 400 activities itself.
- Optional daily context line in new sessions (`inject_daily_context`, off by default), fetched on a short-lived
  background thread with a 3-second budget.
- `/garmin status`, `/garmin today` and `/garmin help`.
- The `garmin-connect:garmin` skill and the **Morning readiness brief** automation blueprint.
- Hermes Desktop panel (**Garmin**): a readiness strip of sleep, overnight HRV, Body Battery, stress, resting
  heart rate and steps, each with a 28-day sparkline placed by date, a badge against your baseline for the
  overnight metrics and today's value so far for running totals; trend signals in plain words; the last activity;
  fitness age against your age; an **Ask Hermes for my readiness brief** button; setup steps with an **Add your API key** button when no key is
  set or it was rejected; and a warning when the key expires within 7 days. Also a full page and two command
  palette entries.
- Read-only backend routes for the panel under `/api/plugins/garmin-connect/`: `overview`, `today`, `trends`,
  `series` and `status`. They answer with a renderable state instead of HTTP errors and never return the key or
  the token.
- Retries with backoff on 429 and 5xx, long ranges split into the per-endpoint windows the API accepted (28 days
  for steps and stress, 31 for sleep, Body Battery and hydration, 120 for HRV and daily summaries, 366 for
  weight), a short in-memory cache, and error messages that say what to do and never contain the key or token.
- `timezone` setting for "today", a dropdown of IANA zones. The default, `auto`, uses the time zone in the
  user's Garmin profile (cached for 12 hours, never holding up a request), then Hermes' configured `timezone`,
  then the server's; an empty value from older configs also means `auto`. `/garmin status` shows the zone in use
  and its source.
- README screenshots of the panel rendered from synthetic data, `SECURITY.md`, `CONTRIBUTING.md`, and CI that
  runs the tests on Python 3.11 and 3.13 and validates the plugin against the latest stable Hermes release.

[0.1.0]: https://github.com/adrianlaborda/hermes-garmin/releases/tag/v0.1.0
