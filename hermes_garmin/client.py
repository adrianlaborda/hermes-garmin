"""Garmin Connect Personal API client: key-to-token exchange, Bearer GETs, retries and a small cache.

The Personal API key is exchanged for a JWT access token that lives 24 hours (or until the key
expires). Both stay in memory only; nothing is written to disk.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import httpx

from .dates import chunks

BASE_URLS = {
    "global": "https://personal.connectapi.garmin.com",
    "china": "https://personal.connectapi.garmin.cn",
}
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
KEY_PAGE = "https://connect.garmin.com/app/settings/personalApiToken"
KEY_ENV = "GARMIN_PERSONAL_API_KEY"
# Where a user stores the key without a terminal: the plugin's settings form in Hermes Desktop.
KEY_SETTINGS = "Hermes Desktop: Settings > Plugins > garmin-connect > Garmin Personal API key"
KEY_CLI = f"hermes config set {KEY_ENV} <key>"

# Re-exchange this long before the token's ``exp`` so a request never races the expiry.
REFRESH_MARGIN_S = 300
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 10.0


class GarminError(Exception):
    """A failure with a message safe to show the user and the model (no secrets).

    ``status`` is the HTTP status when the failure was an HTTP response."""

    def __init__(self, message: str = "", *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class NotConfiguredError(GarminError):
    pass


class AuthError(GarminError):
    pass


class NotFoundError(GarminError):
    pass


class RateLimitedError(GarminError):
    pass


class ApiError(GarminError):
    pass


def key_rejected_message(status: int) -> str:
    return (
        f"Garmin rejected the Personal API key (HTTP {status}). It has probably expired or been deleted. "
        f"Create a new key at {KEY_PAGE} and paste it in {KEY_SETTINGS}, then select Save settings "
        f"(from a terminal: `{KEY_CLI}`). Never paste it into a chat."
    )


def jwt_expiry(token: str) -> Optional[float]:
    """The ``exp`` claim of a JWT, read without verifying the signature; None if absent."""
    try:
        segment = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        return float(payload["exp"])
    except (IndexError, KeyError, TypeError, ValueError, binascii.Error):
        return None


@dataclass(frozen=True)
class Token:
    value: str = field(repr=False)
    expires_at: float


class GarminClient:
    """Thread-safe; one instance can serve concurrent tool calls.

    ``key_provider`` is called on every request so a rotated key takes effect without a restart.
    Tokens and cached responses are keyed by a fingerprint of the key, so two profiles with
    different keys never see each other's data."""

    def __init__(
        self,
        key_provider: Callable[[], Optional[str]],
        base_url: str = BASE_URLS["global"],
        *,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 15.0,
        retries: int = 2,
        cache_ttl: float = 300.0,
        cache_size: int = 256,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._key_provider = key_provider
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport,
                                  headers={"User-Agent": "hermes-garmin"})
        self._retries = retries
        self._cache_ttl = cache_ttl
        self._cache_size = cache_size
        self._clock = clock
        self._sleep = sleep
        self._tokens: dict[str, Token] = {}
        self._token_lock = threading.Lock()
        self._cache: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    # -- credentials -------------------------------------------------------------------------

    def _key(self) -> tuple[str, str]:
        key = (self._key_provider() or "").strip()
        if not key:
            raise NotConfiguredError(
                f"No Garmin Personal API key is configured. Create one at {KEY_PAGE} and paste it in "
                f"{KEY_SETTINGS}, then select Save settings (from a terminal: `{KEY_CLI}`). "
                "Never paste it into a chat."
            )
        return key, hashlib.sha256(f"{self.base_url}\0{key}".encode()).hexdigest()

    def token(self, *, force: bool = False, retries: Optional[int] = None) -> Token:
        key, fingerprint = self._key()
        with self._token_lock:
            current = self._tokens.get(fingerprint)
            if current and not force and current.expires_at - self._clock() > REFRESH_MARGIN_S:
                return current
            headers = {"Accept": "*/*", "Authorization": f"ApiKey {key}"}
            response = self._send("POST", TOKEN_PATH, headers=headers, retries=retries, secrets=(key,))
            if response.status_code in (401, 403):
                self._tokens.pop(fingerprint, None)
                raise AuthError(key_rejected_message(response.status_code))
            self._raise_for_status(response, secrets=(key,))
            value = response.text.strip().strip('"')
            if not value:
                raise ApiError("Garmin's token endpoint returned an empty response.")
            # The official client refuses a token without ``exp``; an hour keeps us working and
            # forces a fresh exchange well inside Garmin's 24 h lifetime.
            expires_at = jwt_expiry(value) or self._clock() + 3600
            token = Token(value, expires_at)
            self._tokens[fingerprint] = token
            return token

    def fingerprint(self) -> str:
        return self._key()[1]

    # -- requests ----------------------------------------------------------------------------

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None, *,
            ttl: Optional[float] = None, retries: Optional[int] = None) -> Any:
        """GET JSON from ``path``. Returns None for 204/empty bodies. Cached for ``ttl`` seconds."""
        fingerprint = self.fingerprint()
        cache_key = (fingerprint, path, tuple(sorted((params or {}).items())))
        ttl = self._cache_ttl if ttl is None else ttl
        cached = self._cache_get(cache_key, ttl)
        if cached is not _MISS:
            return cached
        response = self._authorized("GET", path, params=params, retries=retries,
                                    headers={"Accept": "application/json"})
        if response.status_code == 204 or not response.content.strip():
            data = None
        else:
            try:
                data = response.json()
            except ValueError:
                raise ApiError(f"Garmin returned a response that is not JSON for {path}.") from None
        self._cache_put(cache_key, data)
        return data

    def get_range(self, path: Callable[[date, date], tuple[str, Optional[dict]]], start: date, end: date,
                  max_days: int, *, extract: Optional[Callable[[Any], list]] = None, **kwargs: Any) -> list:
        """Fetch ``start..end`` in windows of at most ``max_days`` and concatenate the items.

        ``path(window_start, window_end)`` returns ``(path, params)``; ``extract`` pulls the item
        list out of each response (default: the response itself when it is a list)."""
        extract = extract or (lambda data: data if isinstance(data, list) else [])
        items: list = []
        for window_start, window_end in chunks(start, end, max_days):
            url, params = path(window_start, window_end)
            items.extend(extract(self.get(url, params, **kwargs)) or [])
        return items

    def download(self, path: str, dest: Path, *, max_bytes: int,
                 accept: str = "application/x-zip-compressed") -> int:
        """Stream ``path`` to ``dest`` (written atomically). Returns the byte count."""
        token = self.token()
        partial = dest.with_suffix(dest.suffix + ".part")
        headers = {"Accept": accept, "Authorization": f"Bearer {token.value}"}
        secrets = (token.value,)
        try:
            with self._http.stream("GET", path, headers=headers) as response:
                if response.status_code >= 400:
                    response.read()
                    self._raise_for_status(response, secrets=secrets)
                size = 0
                with partial.open("wb") as fh:
                    for block in response.iter_bytes():
                        size += len(block)
                        if size > max_bytes:
                            raise ApiError(f"The download is larger than {max_bytes // 1_000_000} MB; stopped.")
                        fh.write(block)
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise ApiError(f"Could not download from Garmin Connect ({type(exc).__name__}).") from None
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        if size == 0:
            partial.unlink(missing_ok=True)
            raise ApiError("Garmin returned an empty file.")
        partial.replace(dest)
        return size

    def _authorized(self, method: str, path: str, *, headers: dict, params=None,
                    retries: Optional[int] = None) -> httpx.Response:
        token = self.token(retries=retries)
        response = self._bearer(method, path, token, headers, params, retries)
        if response.status_code == 401:
            # A token can be revoked before ``exp`` (key deleted, password change): re-exchange once.
            token = self.token(force=True, retries=retries)
            response = self._bearer(method, path, token, headers, params, retries)
            if response.status_code == 401:
                raise AuthError(key_rejected_message(401))
        self._raise_for_status(response, secrets=(token.value,))
        return response

    def _bearer(self, method, path, token: Token, headers, params, retries) -> httpx.Response:
        auth = {**headers, "Authorization": f"Bearer {token.value}"}
        return self._send(method, path, headers=auth, params=params, retries=retries, secrets=(token.value,))

    def _send(self, method: str, path: str, *, headers: dict, params=None,
              retries: Optional[int] = None, secrets: tuple = ()) -> httpx.Response:
        attempts = 1 + (self._retries if retries is None else retries)
        attempt = 0
        while True:
            last = attempt + 1 >= attempts
            try:
                response = self._http.request(method, path, headers=headers, params=params)
            except httpx.HTTPError as exc:
                if last:
                    raise ApiError(f"Could not reach Garmin Connect ({type(exc).__name__}). "
                                   "Check the network and try again.") from None
                delay = _backoff(attempt)
            else:
                if response.status_code not in RETRY_STATUSES or last:
                    return response
                delay = _retry_after(response) or _backoff(attempt)
            self._sleep(delay)
            attempt += 1

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, secrets: tuple = ()) -> None:
        status = response.status_code
        if status < 400:
            return
        detail = _error_detail(response, secrets)
        if status == 429:
            raise RateLimitedError("Garmin is rate-limiting requests (HTTP 429). Wait a minute and try again.",
                                   status=status)
        if status == 403:
            raise ApiError("Garmin refused access to this resource (HTTP 403). It may belong to another account."
                           + detail, status=status)
        if status == 404:
            raise NotFoundError("Garmin has no such item (HTTP 404)." + detail, status=status)
        if status >= 500:
            raise ApiError(f"Garmin Connect had a server error (HTTP {status}). Try again later; a shorter date "
                           "range sometimes helps." + detail, status=status)
        raise ApiError(f"Garmin rejected the request (HTTP {status})." + detail, status=status)

    # -- cache -------------------------------------------------------------------------------

    def _cache_get(self, key: tuple, ttl: float) -> Any:
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is None or ttl <= 0 or self._clock() - hit[0] > ttl:
                return _MISS
            self._cache.move_to_end(key)
            return hit[1]

    def _cache_put(self, key: tuple, value: Any) -> None:
        with self._cache_lock:
            self._cache[key] = (self._clock(), value)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)


_MISS = object()


def _backoff(attempt: int) -> float:
    return min(0.5 * 2 ** attempt, 4.0)


def _retry_after(response: httpx.Response) -> Optional[float]:
    try:
        return min(float(response.headers.get("Retry-After", "")), MAX_RETRY_AFTER_S)
    except ValueError:
        return None


def redact(text: str, secrets: tuple) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _error_detail(response: httpx.Response, secrets: tuple) -> str:
    """Garmin's own error message, if it sent one, trimmed and with any credential removed."""
    try:
        body = response.json()
    except ValueError:
        body = None
    message = ""
    if isinstance(body, dict):
        message = str(body.get("clientMessage") or body.get("message") or body.get("error") or "")
    message = redact(message.strip(), secrets)[:200]
    return f" Garmin says: {message}" if message else ""
