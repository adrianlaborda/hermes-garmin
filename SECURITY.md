# Security policy

## Supported versions

Only the latest release gets security fixes.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/adrianlaborda/hermes-garmin/security/advisories/new).

Include what an attacker can do, the steps to reproduce and the plugin and Hermes Agent versions. You will get an
answer within a week. Once a fix is released, the advisory is published and you are credited unless you prefer
otherwise.

Never include a real Garmin Personal API key, access token or health data in a report. Synthetic values are enough.

## What the plugin handles

- **The Garmin Personal API key.** Read through Hermes' secret route (`get_secret`, per profile) on each request,
  stored by Hermes in the profile's `.env`. The plugin never writes, logs or returns it, and never reads it from files.
- **The access token.** Exchanged from the key, kept in memory only, renewed before it expires. Never logged,
  written to disk or returned by the panel routes. Error messages are scrubbed of the key and the token.
- **Your health and activity data.** Fetched read-only from `personal.connectapi.garmin.com` (or `.cn`), cached in
  memory for 5 minutes, and passed to the model provider you chose only when a tool runs or, if you turn it on,
  in the one-line daily context. No telemetry, no other host.
- **Activity files.** `garmin_activity_fit` saves original activity files under
  `<HERMES_HOME>/plugin-data/garmin/activity-files/`, with a 50 MB limit per file.
- **The Desktop panel's backend routes** (`/api/plugins/garmin-connect/`). Read-only, served by Hermes for enabled
  plugins, and they never return the key or the token.

## Out of scope

- Garmin's own services, accounts and key management. Report those to Garmin.
- Hermes Agent itself. Report those to [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
- What the model provider you chose does with data you send it.
