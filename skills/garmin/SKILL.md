---
name: garmin
description: Answer questions about the user's sleep, recovery, HRV, Body Battery, stress, steps, heart rate, workouts and weight from their Garmin watch, and write readiness briefs. Use when the user asks how they slept, whether they are recovered or should train, about a run or ride, or about trends in their health metrics.
---

# Garmin Connect data

The `garmin_*` tools read the user's own Garmin Connect data through Garmin's Personal API. They are
read-only. The data is personal health information: use it to answer the user, do not repeat more of
it than the question needs, and never share it with third parties or other tools unless asked.

## Which tool

| Question | Tool |
|---|---|
| "How am I today?", "Did I sleep well?", "Should I train?" | `garmin_today`, then `garmin_trends` |
| Sleep over several nights | `garmin_sleep` with `days` |
| Recovery, fatigue, "am I overdoing it?", progress over weeks | `garmin_trends` (default 28 days) |
| HRV status or values | `garmin_hrv` |
| Energy through the day | `garmin_body_battery` (one day includes the events that moved it) |
| Stress | `garmin_stress` |
| Steps, activity level | `garmin_steps` |
| Resting or hourly heart rate | `garmin_heart_rate` (`hourly: true` for one day) |
| "My last run", workouts in a period, by type or name | `garmin_activities` (`activity_type` such as `running` or `strength_training`), then `garmin_activity` with its `id` |
| Laps or second-by-second analysis | `garmin_activity_fit` (gives a local ZIP path for other tools) |
| Weight, body composition, water intake | `garmin_body` |

Dates are the user's local calendar days. A night of sleep is filed under the morning the user woke
up, so "last night" is today's date. Data appears after the watch syncs; early in the morning today's
values may be missing or partial, and today's steps and stress are running totals.

**Check the sync time.** `garmin_today` returns `synced_local`, when the watch last synced. Today's
steps, stress, Body Battery and intensity minutes stop at that moment. When `freshness_note` is present
the sync is hours old: say the numbers are "as of HH:MM", never conclude the user was inactive or
sedentary from them, and suggest opening Garmin Connect on the phone to sync. A Body Battery with
"drained 0" right after waking means the same thing.

Prefer the summaries. Ask for `raw: true` only when a field you need is missing; it is large.

## Interpreting the numbers

- **Trends before conclusions.** One night says little. Before saying the user is fatigued,
  recovered, improving or declining, call `garmin_trends` and base the answer on its flags, which
  compare the latest value with the user's own baseline (z-score) and the 7-day with the 28-day
  average. Quote the flag text and the numbers. If there are no flags, say the metrics are within
  their usual range.
- **HRV** (overnight RMSSD, ms) is highly individual: 30 ms can be normal for one person and low for
  another. Compare with the user's balanced range and baseline, never with population figures.
  Garmin's HRV status (BALANCED, UNBALANCED, LOW) rates the **7-day average**, not last night: a
  BALANCED week can include a night below the range. Use `last_night_vs_range` and `reading` and
  keep the two apart ("your 7-day HRV is balanced; last night was below your usual range"). A drop
  over several days, together with a higher resting heart rate, is a common sign of accumulated
  fatigue, illness coming on, alcohol or poor sleep. A single low night is usually noise.
- **Resting heart rate** a few bpm above baseline for several days points the same way as low HRV.
- **Body Battery** (0-100) is Garmin's energy estimate from HRV, stress and activity. The value at
  wake shows how well sleep recharged; "charged" during sleep below about 30 suggests poor recovery.
- **Stress** (0-100) is physiological, derived from heart rate variability while awake, not
  emotional stress: 0-25 rest, 26-50 low, 51-75 medium, 76-100 high. Exercise is excluded.
- **Sleep score** (0-100) combines duration, stages and restlessness. Adults usually need 7-9 h.
  Deep and REM shares vary a lot between people and nights; avoid over-reading single nights.
- **Training effect** (0-5) rates the aerobic and anaerobic load of one activity; training load is
  its overall cost.

## Readiness brief

When asked for a brief (or run by the morning readiness automation): call `garmin_today` and
`garmin_trends`, then give a few short lines: sleep, HRV and resting HR against baseline, Body
Battery at wake, any unfavourable flags, and one practical suggestion (for example "keep today easy"
or "good day for the planned intervals"). Base the suggestion on the flags, not on a single value.

## Safety

- This is not medical advice and Garmin's metrics are estimates. Do not diagnose.
- If the data shows something that could be a health issue (very high or very low resting heart rate,
  repeated low SpO2 during sleep, sudden large changes that persist), say so plainly and suggest
  talking to a doctor, without alarming the user.
- Do not suggest training through illness signs (fever, chest pain, unusual shortness of breath).

## Errors

- **Key missing or rejected:** the user needs a new Personal API key from Garmin Connect Web
  (Settings, Personal API Key; it is shown once and expires). In Hermes Desktop they paste it in
  Settings > Plugins > garmin-connect > Garmin Personal API key and select Save settings (the Garmin
  panel's "Add your API key" button opens that page); from a terminal,
  `hermes config set GARMIN_PERSONAL_API_KEY <key>`. Never ask the user to paste the key into the
  chat: chat messages go to the model provider.
- **No data:** the watch may not have synced, or the metric needs a compatible device.
- **Rate limit or server errors:** wait and retry later with a shorter range.
- `/garmin status` shows the key and token state.
