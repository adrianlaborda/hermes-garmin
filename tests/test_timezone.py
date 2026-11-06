"""The ``timezone`` setting: a dropdown whose default, ``auto``, follows the Garmin profile."""

from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import yaml

from hermes_garmin import commands, runtime as runtime_mod
from hermes_garmin.dates import known_zone
from hermes_garmin.runtime import Runtime, profile_zone_name

SETTINGS = "/userprofile-service/userprofile/settings"
MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / "plugin.yaml").read_text())


def test_manifest_offers_auto_then_every_canonical_zone():
    spec = MANIFEST["config_schema"]["timezone"]
    choices = spec["choices"]
    assert spec["default"] == "auto" and choices[:2] == ["auto", "UTC"]
    zones = choices[2:]
    assert zones == sorted(set(zones)) and len(zones) > 400
    assert {"Europe/Madrid", "Europe/Paris", "Europe/Oslo", "America/New_York", "Asia/Kolkata"} <= set(zones)
    assert not [z for z in zones if z.split("/")[0] in {"Etc", "US", "SystemV"} or "/" not in z]
    unknown = [z for z in zones if not known_zone(z)]
    assert len(unknown) < 5, unknown  # a much older tz database may lack the newest names


@pytest.fixture
def make_rt(fake, make_client, monkeypatch):
    monkeypatch.setattr(runtime_mod, "_hermes_timezone", lambda: "America/Chicago")

    def build(**settings):
        rt = Runtime(lambda key, default=None: settings.get(key, default), key_provider=lambda: "k")
        client = make_client()
        rt._clients = {client.base_url: client}
        return rt
    return build


@pytest.mark.parametrize("stored", ["", "auto", "AUTO", None, "Not/AZone"])
def test_auto_empty_or_unknown_use_the_garmin_profile(fake, make_rt, stored):
    fake.routes[SETTINGS] = {"timeZone": "Europe/Paris", "measurementSystem": "metric"}
    choice = make_rt(timezone=stored).zone_choice()
    assert choice.tz == ZoneInfo("Europe/Paris") and choice.source == "garmin"
    assert choice.describe() == "Europe/Paris (from your Garmin profile)"


def test_an_explicit_zone_wins_without_asking_garmin(fake, make_rt):
    fake.routes[SETTINGS] = {"timeZone": "Europe/Paris"}
    choice = make_rt(timezone="Europe/Madrid").zone_choice()
    assert (choice.name, choice.source) == ("Europe/Madrid", "setting")
    assert not fake.calls(SETTINGS)


def test_profile_zone_is_cached(fake, make_rt):
    fake.routes[SETTINGS] = {"timeZone": "Europe/Paris"}
    rt = make_rt(timezone="auto")
    for _ in range(3):
        assert rt.tz() == ZoneInfo("Europe/Paris")
    assert len(fake.calls(SETTINGS)) == 1


def test_profile_failure_falls_through_to_hermes_then_backs_off(fake, make_rt):
    fake.routes[SETTINGS] = httpx.Response(500)
    rt = make_rt(timezone="auto")
    assert rt.zone_choice().describe() == "America/Chicago (from Hermes' timezone setting)"
    calls = len(fake.calls(SETTINGS))
    rt.zone_choice()
    assert len(fake.calls(SETTINGS)) == calls  # no retry inside the back-off


def test_without_a_key_or_hermes_zone_uses_the_server(make_rt, monkeypatch):
    monkeypatch.setattr(runtime_mod, "_hermes_timezone", lambda: "")
    rt = make_rt()
    rt._key_provider = lambda: None
    assert rt.zone_choice().source == "server"


def test_a_slow_profile_never_blocks(fake, make_rt):
    import threading
    gate = threading.Event()
    fake.routes[SETTINGS] = lambda req: (gate.wait(2), {"timeZone": "Europe/Paris"})[1]
    rt = make_rt(timezone="auto")
    assert rt.zone_choice(wait=0.05).source == "hermes"
    gate.set()
    rt._profile_inflight[next(iter(rt._profile_inflight))].join(2)
    assert rt.zone_choice(wait=0).source == "garmin"


def test_profile_zone_name():
    assert profile_zone_name({"timeZone": "gmt"}) == "UTC"
    assert profile_zone_name({"timeZone": "Atlantic/South_Georgia"}) == "Atlantic/South_Georgia"
    assert profile_zone_name({"timeZone": "nowhere"}) == "" == profile_zone_name(None)


def test_status_shows_the_zone_and_its_source(fake, runtime, monkeypatch):
    fake.routes[SETTINGS] = {"timeZone": "Europe/Paris"}
    monkeypatch.setattr(runtime, "_profile_zones", {})
    monkeypatch.setattr(runtime, "_profile_failed", {})
    monkeypatch.setattr(runtime, "_profile_inflight", {})
    assert "Time zone: Europe/Paris (from your Garmin profile)" in commands.handle("status")
