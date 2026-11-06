"""Shared fixtures: a fake Garmin Personal API behind ``httpx.MockTransport``. No network."""

from __future__ import annotations

import base64
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_garmin import reports  # noqa: E402
from hermes_garmin.client import TOKEN_PATH, GarminClient  # noqa: E402
from hermes_garmin.runtime import RUNTIME  # noqa: E402

TODAY = date(2026, 11, 6)
# 16:00 in Madrid; the fixture's watch last synced at 10:12 local, so today's totals are stale.
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
TZ = ZoneInfo("Europe/Madrid")
API_KEY = "test-personal-api-key-0123456789"
FIXTURES = Path(__file__).parent / "fixtures"


def make_jwt(exp: float | None) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    claims = {"sub": "user"} if exp is None else {"sub": "user", "exp": exp}
    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class Clock:
    def __init__(self, now: float = 1_793_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Seq(list):
    """Route answers consumed one per request; the last one repeats."""


class FakeGarmin:
    """Routes by path. A route is a value (JSON body), an ``httpx.Response``, a callable
    ``(request) -> value | Response`` or a ``Seq`` of those."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.routes: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []
        self.token_exp_offset = 86_400
        self.routes[TOKEN_PATH] = lambda req: httpx.Response(
            200, text=make_jwt(self.clock.now + self.token_exp_offset))

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if isinstance(route, Seq):
            route = route.pop(0) if len(route) > 1 else route[0]
        if route is None:
            return httpx.Response(404)
        if callable(route):
            route = route(request)
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(200, json=route)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake(clock) -> FakeGarmin:
    return FakeGarmin(clock)


@pytest.fixture
def sleeps() -> list:
    return []


@pytest.fixture
def make_client(fake, clock, sleeps) -> Callable[..., GarminClient]:
    def build(key: Callable[[], str | None] = lambda: API_KEY, **kwargs) -> GarminClient:
        return GarminClient(key, transport=httpx.MockTransport(fake.handler), clock=clock,
                            sleep=sleeps.append, **kwargs)
    return build


@pytest.fixture
def settings() -> dict[str, Any]:
    """Plugin settings as ``ctx.get_config`` would return them."""
    return {}


@pytest.fixture
def runtime(monkeypatch, make_client, settings):
    """Point the plugin's shared runtime at the fake server, a fixed day and zone."""
    client = make_client()
    monkeypatch.setattr(RUNTIME, "_clients", {client.base_url: client})
    monkeypatch.setattr(RUNTIME, "_key_provider", lambda: API_KEY)
    monkeypatch.setattr(RUNTIME, "today", lambda: TODAY)
    monkeypatch.setattr(RUNTIME, "tz", lambda: TZ)
    monkeypatch.setattr(RUNTIME, "_get_config", lambda key, default=None: settings.get(key, default))
    monkeypatch.setattr(reports, "_now", lambda: NOW)
    return RUNTIME
