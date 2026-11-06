"""Plugin settings, the API key route and the shared client.

The key is read through Hermes' secret-scope API on every request, which honours the active
profile; the plugin never parses ``.env`` itself and never caches the key.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from datetime import date, datetime, tzinfo
from typing import Any, Callable, NamedTuple, Optional

from . import endpoints
from .client import BASE_URLS, KEY_ENV, GarminClient
from .dates import known_zone, zone

logger = logging.getLogger(__name__)

AUTO_ZONE = "auto"
# The zone in the user's Garmin profile rarely changes; failures are retried after a pause.
PROFILE_ZONE_TTL_S = 12 * 3600.0
PROFILE_ZONE_RETRY_S = 600.0
# How long a request waits for the profile before falling through to Hermes' zone. A slower
# answer still lands in the cache for the next one.
PROFILE_ZONE_BUDGET_S = 2.0

ZONE_SOURCES = {
    "setting": "set in the plugin settings",
    "garmin": "from your Garmin profile",
    "hermes": "from Hermes' timezone setting",
    "server": "the server's local time",
}


class ZoneChoice(NamedTuple):
    tz: tzinfo
    name: str
    source: str  # a ZONE_SOURCES key

    def describe(self) -> str:
        return f"{self.name} ({ZONE_SOURCES[self.source]})"

ConfigGetter = Callable[[str, Any], Any]


def _clean(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def read_api_key() -> Optional[str]:
    """The key from the active profile's secret scope.

    Agent turns, cron fires and the dashboard bind that scope. A slash command dispatched from
    Desktop or the TUI (``command.dispatch``) binds only the session's Hermes home, so there
    ``get_secret`` raises on a multi-profile host (fail closed), or on a single-profile one reads
    the process env as it was at launch, which lacks a key saved later. Only when NO scope is
    bound is the profile's scope rebuilt, through Hermes' own ``build_profile_secret_scope``, for
    the home Hermes bound for the call (``get_hermes_home``): the profile whose config and skills
    that command already resolves against. A bound scope stays authoritative."""
    try:
        from agent import secret_scope
    except Exception:  # outside Hermes
        return None
    try:
        value = _clean(secret_scope.get_secret(KEY_ENV))
    except Exception:  # UnscopedSecretError: no profile scope while multiplexing
        value = None
    if value is not None:
        return value
    try:
        if secret_scope.current_secret_scope() is not None:
            return None
        from hermes_constants import get_hermes_home
        return _clean(secret_scope.build_profile_secret_scope(get_hermes_home()).get(KEY_ENV))
    except Exception:
        return None


def key_configured() -> bool:
    """Passive check for ``check_fn``: no network, no installs."""
    return read_api_key() is not None


def _hermes_timezone() -> str:
    """The active profile's ``timezone`` from its ``config.yaml``, through Hermes' public config API.

    Not an environment variable: under the multiplexed gateway ``HERMES_TIMEZONE`` holds only the
    default profile's value, and the plugin declares no env var besides its API key."""
    try:
        from hermes_cli.config import load_config_readonly
        return str((load_config_readonly() or {}).get("timezone") or "").strip()
    except Exception:
        return ""


PLUGIN_ID = "garmin-connect"


def hermes_plugin_setting(key: str, default: Any = None) -> Any:
    """``plugins.entries.garmin-connect.settings.<key>`` from the active profile's config.

    The values ``ctx.get_config`` returns, for code that runs without a plugin context (the
    Desktop panel's backend routes are imported by the web server, not through ``register``)."""
    try:
        from hermes_cli.config import load_config_readonly
        entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
    except Exception:
        return default
    entry = entries.get(PLUGIN_ID) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return default
    for section in ("settings", "config"):
        values = entry.get(section)
        if isinstance(values, dict) and key in values:
            return values[key]
    return default


class Runtime:
    def __init__(self, get_config: Optional[ConfigGetter] = None,
                 key_provider: Callable[[], Optional[str]] = read_api_key) -> None:
        self._get_config = get_config or (lambda _key, default=None: default)
        self._key_provider = key_provider
        self._clients: dict[str, GarminClient] = {}
        self._lock = threading.Lock()
        self._clock = time.monotonic
        # key fingerprint -> (fetched at, IANA name or "" when the profile has none)
        self._profile_zones: dict[str, tuple[float, str]] = {}
        self._profile_failed: dict[str, float] = {}
        self._profile_inflight: dict[str, threading.Thread] = {}

    def bind(self, get_config: ConfigGetter) -> None:
        self._get_config = get_config

    def setting(self, key: str, default: Any = None) -> Any:
        try:
            value = self._get_config(key, default)
        except Exception:
            return default
        return default if value is None else value

    def flag(self, key: str, default: bool = False) -> bool:
        value = self.setting(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def api_key(self) -> Optional[str]:
        return self._key_provider()

    def has_key(self) -> bool:
        return bool(self.api_key())

    @property
    def region(self) -> str:
        region = str(self.setting("region", "global")).strip().lower()
        return region if region in BASE_URLS else "global"

    def client(self) -> GarminClient:
        base_url = BASE_URLS[self.region]
        with self._lock:
            client = self._clients.get(base_url)
            if client is None:
                client = self._clients[base_url] = GarminClient(self._key_provider, base_url)
            return client

    def zone_choice(self, *, wait: float = PROFILE_ZONE_BUDGET_S) -> ZoneChoice:
        """The zone that decides "today", and where it came from: the plugin setting, unless it is
        ``auto`` or empty; then the zone in the user's Garmin profile; then Hermes' ``timezone``;
        then the server's local zone. Never raises and never waits more than ``wait`` seconds."""
        setting = str(self.setting("timezone", "") or "").strip()
        if setting and setting.lower() != AUTO_ZONE and known_zone(setting):
            return ZoneChoice(zone(setting), setting, "setting")
        garmin = self._garmin_zone(wait)
        if garmin:
            return ZoneChoice(zone(garmin), garmin, "garmin")
        hermes = _hermes_timezone()
        if hermes and known_zone(hermes):
            return ZoneChoice(zone(hermes), hermes, "hermes")
        local = zone(None)
        return ZoneChoice(local, getattr(local, "key", None) or datetime.now(local).tzname() or "UTC", "server")

    def tz(self) -> tzinfo:
        return self.zone_choice().tz

    # -- the Garmin profile's zone ------------------------------------------------------------

    def _garmin_zone(self, wait: float) -> str:
        if not self.has_key():
            return ""
        try:
            client = self.client()
            fingerprint = client.fingerprint()
        except Exception:
            return ""
        now = self._clock()
        with self._lock:
            hit = self._profile_zones.get(fingerprint)
            if hit and now - hit[0] < PROFILE_ZONE_TTL_S:
                return hit[1]
            failed = self._profile_failed.get(fingerprint)
            if failed is not None and now - failed < PROFILE_ZONE_RETRY_S:
                return hit[1] if hit else ""
            worker = self._profile_inflight.get(fingerprint)
            if worker is None or not worker.is_alive():
                # A copied context keeps Hermes' per-profile secret scope visible to the worker.
                worker = threading.Thread(target=contextvars.copy_context().run,
                                          args=(self._fetch_garmin_zone, client, fingerprint),
                                          name="garmin-zone", daemon=True)
                self._profile_inflight[fingerprint] = worker
                worker.start()
        worker.join(max(0.0, wait))
        with self._lock:
            hit = self._profile_zones.get(fingerprint)
        return hit[1] if hit else ""

    def _fetch_garmin_zone(self, client: GarminClient, fingerprint: str) -> None:
        try:
            name = profile_zone_name(endpoints.user_settings(client, retries=0, ttl=0))
        except Exception as exc:  # a missing zone must never break a request
            logger.debug("garmin profile zone unavailable: %s", exc)
            with self._lock:
                self._profile_failed[fingerprint] = self._clock()
            return
        with self._lock:
            self._profile_zones[fingerprint] = (self._clock(), name)
            self._profile_failed.pop(fingerprint, None)

    def today(self) -> date:
        return datetime.now(self.tz()).date()

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()


def profile_zone_name(settings: Any) -> str:
    """The IANA zone in Garmin's ``UserSettingsDTO.timeZone``, or "" when absent or unknown.
    Garmin writes GMT as ``gmt``."""
    name = str((settings or {}).get("timeZone") or "").strip() if isinstance(settings, dict) else ""
    if name.lower() in {"gmt", "utc", "etc/gmt", "etc/utc"}:
        return "UTC"
    return name if known_zone(name) else ""


RUNTIME = Runtime()
