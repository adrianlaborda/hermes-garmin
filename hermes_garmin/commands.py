"""The ``/garmin`` slash command: ``status``, ``today`` and ``help``."""

from __future__ import annotations

import logging
from datetime import datetime

from . import __version__, reports
from .client import KEY_CLI, KEY_ENV, KEY_PAGE, KEY_SETTINGS, GarminError, jwt_expiry
from .runtime import RUNTIME

logger = logging.getLogger(__name__)

HELP = f"""/garmin status  key, server, time zone and access-token check
/garmin today   today's sleep, HRV, Body Battery, stress, steps and last activity
/garmin help    this text

Set up:
1. Create a Personal API key at {KEY_PAGE}
   (Garmin shows it once; it expires after the 30 to 365 days you pick).
2. Paste it in {KEY_SETTINGS}
   and select Save settings. Using a terminal instead: `{KEY_CLI}`.
   Never paste the key into a chat.
Then ask Hermes things like
"how did I sleep this week?" or "am I recovered enough for intervals today?"."""


def _when(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, RUNTIME.tz()).strftime("%Y-%m-%d %H:%M")


def status() -> str:
    region = RUNTIME.region
    client = RUNTIME.client()
    lines = [f"Garmin Connect plugin {__version__} (unofficial)",
             f"Server: {region} ({client.base_url.removeprefix('https://')})"]
    key = RUNTIME.api_key()
    if not key:
        lines += [f"API key: not set. Create one at {KEY_PAGE}",
                  f"and paste it in {KEY_SETTINGS} (from a terminal: `{KEY_CLI}`)."]
        return "\n".join(lines)
    lines.append(f"API key: set ({KEY_ENV})")
    key_expiry = jwt_expiry(key)
    lines.append(f"Key expires: {_when(key_expiry)}" if key_expiry else
                 "Key expiry: not readable from the key. Garmin emails you about a week before it expires.")
    try:
        token = client.token()
        lines.append(f"Access token: OK, valid until {_when(token.expires_at)} (renewed automatically)")
    except GarminError as exc:
        lines.append(f"Access token: failed. {exc}")
    lines.append(f"Time zone: {RUNTIME.zone_choice().describe()}")
    injecting = RUNTIME.flag("inject_daily_context", False)
    lines.append(f"Daily context in chats: {'on' if injecting else 'off'} "
                 "(setting plugins.entries.garmin-connect.settings.inject_daily_context)")
    return "\n".join(lines)


def today() -> str:
    try:
        return reports.human_text(reports.today(RUNTIME.client(), RUNTIME.today()))
    except GarminError as exc:
        return str(exc)


def handle(raw_args: str = "") -> str:
    words = (raw_args or "").strip().lower().split(maxsplit=1)
    sub = words[0] if words else "status"
    try:
        if sub == "status":
            return status()
        if sub == "today":
            return today()
    except Exception as exc:
        logger.exception("/garmin %s failed", sub)
        return f"/garmin {sub} failed ({type(exc).__name__})."
    return HELP
