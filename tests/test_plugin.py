"""Registration against a recording context, and agreement with plugin.yaml."""

import importlib.util
import inspect
import os
import sys
import types
from pathlib import Path

import httpx
import pytest
import yaml

from conftest import API_KEY, ROOT, TODAY, fixture
from hermes_garmin import commands
from hermes_garmin.client import TOKEN_PATH

MANIFEST = yaml.safe_load((ROOT / "plugin.yaml").read_text())


class RecordingContext:
    def __init__(self):
        self.tools, self.hooks, self.commands, self.skills, self.blueprints = {}, {}, {}, {}, {}
        self.unload = []

    def get_config(self, key, default=None):
        return default

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None, emoji="", **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, "check_fn": check_fn,
                            "requires_env": requires_env}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description="", args_hint="", **kw):
        self.commands[name] = handler

    def register_skill(self, name, path, description="", **kw):
        self.skills[name] = Path(path)

    def register_automation_blueprint(self, key, **fields):
        self.blueprints[key] = fields

    def on_unload(self, callback):
        self.unload.append(callback)


def _load_and_register():
    spec = importlib.util.spec_from_file_location("hermes_plugins_garmin", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # the same entry point Hermes' loader imports
    recording = RecordingContext()
    module.register(recording)
    return recording


@pytest.fixture
def ctx():
    return _load_and_register()


def test_registrations_match_the_manifest(ctx):
    assert sorted(ctx.tools) == sorted(MANIFEST["provides_tools"])
    assert sorted(ctx.hooks) == sorted(MANIFEST["provides_hooks"])
    required = [e["name"] for e in MANIFEST["requires_env"]]
    assert all(t["requires_env"] == required for t in ctx.tools.values())
    assert set(MANIFEST["config_schema"]) == {"api_key", "region", "timezone", "inject_daily_context"}
    # The Desktop settings form stores the key under the same env name the plugin reads.
    api_key = MANIFEST["config_schema"]["api_key"]
    assert api_key["type"] == "secret" and api_key["env"] == "GARMIN_PERSONAL_API_KEY" == required[0]
    assert list(MANIFEST["config_schema"])[0] == "api_key"
    assert MANIFEST["config_schema"]["inject_daily_context"]["default"] is False


def test_tool_schemas(ctx):
    for name, tool in ctx.tools.items():
        schema = tool["schema"]
        assert tool["toolset"] == "garmin" and schema["name"] == name
        assert len(schema["description"]) > 40
        params = schema["parameters"]
        assert params["type"] == "object" and set(params["required"]) <= set(params["properties"])
        assert "**_kwargs" in str(inspect.signature(tool["handler"]))


class FakeSecretScope:
    """``agent.secret_scope`` and ``hermes_constants`` as Hermes exposes them to plugins: a bound
    scope (or none), ``get_secret`` with its fail-closed raise, and per-home ``.env`` scopes."""

    def __init__(self, monkeypatch) -> None:
        self.bound = None          # the installed scope mapping, None when no scope is bound
        self.environ = {}          # the process env as it was at launch
        self.multiplex = True      # a multi-profile host: unscoped reads fail closed
        self.homes = {}            # home -> what build_profile_secret_scope(home) returns
        self.home = "/home/hermes/.hermes"
        self.built = []
        module = types.ModuleType("agent.secret_scope")
        module.get_secret = self.get_secret
        module.current_secret_scope = lambda: self.bound
        module.build_profile_secret_scope = self.build
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path(self.home)
        agent = types.ModuleType("agent")
        agent.secret_scope = module
        monkeypatch.setitem(sys.modules, "agent", agent)
        monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
        monkeypatch.setitem(sys.modules, "hermes_constants", constants)

    def get_secret(self, name, default=None):
        if self.bound is not None:
            return self.bound.get(name, default if self.multiplex else self.environ.get(name, default))
        if self.multiplex:
            raise RuntimeError("UnscopedSecretError: no profile secret scope while multiplexing")
        return self.environ.get(name, default)

    def build(self, home):
        self.built.append(str(home))
        if str(home) == "broken":
            raise OSError("unreadable")
        return dict(self.homes.get(str(home), {}))


def test_key_is_read_through_get_secret(ctx, monkeypatch):
    fake = FakeSecretScope(monkeypatch)
    fake.bound = {}
    check = next(iter(ctx.tools.values()))["check_fn"]
    assert check() is False
    fake.bound["GARMIN_PERSONAL_API_KEY"] = "  "
    assert check() is False
    fake.bound["GARMIN_PERSONAL_API_KEY"] = API_KEY
    assert check() is True
    assert fake.built == []  # a scoped read never rebuilds anything


def test_a_bound_scope_is_authoritative(ctx, monkeypatch):
    fake = FakeSecretScope(monkeypatch)
    fake.bound = {}
    fake.homes[fake.home] = {"GARMIN_PERSONAL_API_KEY": API_KEY}
    check = next(iter(ctx.tools.values()))["check_fn"]
    assert check() is False and fake.built == []


def test_slash_command_without_a_scope_reads_the_bound_homes_scope(runtime, monkeypatch):
    """Desktop's ``command.dispatch`` binds the session's home but no secret scope: on a
    multi-profile host ``get_secret`` raises, so ``/garmin`` said "no key" right after a save."""
    from hermes_garmin import runtime as runtime_mod

    monkeypatch.setattr(runtime, "_key_provider", runtime_mod.read_api_key)
    fake = FakeSecretScope(monkeypatch)
    fake.homes[fake.home] = {"GARMIN_PERSONAL_API_KEY": f" {API_KEY} "}
    fake.homes["/home/hermes/.hermes/profiles/other"] = {}
    assert runtime_mod.read_api_key() == API_KEY and fake.built == [fake.home]
    assert "API key: set" in commands.handle("status")
    fake.home = "/home/hermes/.hermes/profiles/other"  # a secondary profile's session
    assert runtime_mod.read_api_key() is None  # never the launch profile's key
    assert "API key: not set" in commands.handle("status")
    fake.home = "broken"
    assert runtime_mod.read_api_key() is None


def test_single_profile_host_sees_a_key_saved_after_launch(monkeypatch):
    from hermes_garmin import runtime

    fake = FakeSecretScope(monkeypatch)
    fake.multiplex = False
    assert runtime.read_api_key() is None
    fake.homes[fake.home] = {"GARMIN_PERSONAL_API_KEY": API_KEY}  # saved to .env, env still stale
    assert runtime.read_api_key() == API_KEY
    fake.environ["GARMIN_PERSONAL_API_KEY"] = "env-key"  # systemd / op run injection still wins
    assert runtime.read_api_key() == "env-key"


def test_outside_hermes_there_is_no_key(monkeypatch):
    from hermes_garmin import runtime

    monkeypatch.setitem(sys.modules, "agent", None)
    assert runtime.read_api_key() is None


def test_key_check_is_not_cached_by_hermes(monkeypatch):
    marked = []
    registry = types.ModuleType("tools.registry")
    registry.no_cache_check_fn = lambda fn: marked.append(fn) or fn
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.registry", registry)
    recording = _load_and_register()
    from hermes_garmin.runtime import key_configured
    assert marked and all(t["check_fn"] in marked for t in recording.tools.values())
    assert marked[0].__name__ == key_configured.__name__


def test_hook_accepts_hermes_kwargs(ctx):
    hook = ctx.hooks["pre_llm_call"]
    assert "kwargs" in str(inspect.signature(hook))


def test_command_skill_blueprint(ctx):
    assert "garmin" in ctx.commands
    assert ctx.skills["garmin"].read_text().startswith("---\nname: garmin\n")
    blueprint = ctx.blueprints["morning-readiness"]
    assert "garmin_trends" in blueprint["prompt_template"]
    assert blueprint["skills"] == ("garmin-connect:garmin",)


def _hermes_source():
    for candidate in (os.environ.get("HERMES_AGENT_SRC"), Path.home() / ".hermes" / "hermes-agent"):
        if candidate and (Path(candidate) / "cron" / "blueprint_plugins.py").exists():
            return str(candidate)
    return None


@pytest.mark.skipif(_hermes_source() is None, reason="needs a Hermes Agent checkout (HERMES_AGENT_SRC)")
def test_blueprint_is_accepted_by_hermes(ctx, monkeypatch):
    monkeypatch.syspath_prepend(_hermes_source())
    pytest.importorskip("ruamel.yaml", reason="run with Hermes' own interpreter to include this check")
    from cron.blueprint_catalog import fill_blueprint
    from cron.blueprint_plugins import build_plugin_blueprint

    blueprint = build_plugin_blueprint("garmin-connect", "morning-readiness", **ctx.blueprints["morning-readiness"])
    filled = fill_blueprint(blueprint, {"time": "06:45", "recurrence": "weekdays"})
    assert filled["schedule"] == "45 6 * * 1-5"
    assert "general fitness" in filled["prompt"]


def test_status_command(fake, runtime):
    out = commands.handle("status")
    assert "API key: set" in out and "Access token: OK" in out and API_KEY not in out
    assert "Daily context in chats: off" in out
    fake.routes[TOKEN_PATH] = httpx.Response(401)
    runtime.client()._tokens.clear()
    assert "Access token: failed. Garmin rejected" in commands.handle("status")


def test_status_without_key(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_key_provider", lambda: None)
    out = commands.handle("")
    assert "API key: not set" in out
    assert out.index("Settings > Plugins > garmin-connect") < out.index("hermes config set")


def test_help(runtime):
    out = commands.handle("help")
    assert "/garmin today" in out
    assert out.index("Settings > Plugins > garmin-connect") < out.index("hermes config set")
    assert commands.handle("nonsense") == commands.HELP


def test_today_command(fake, runtime):
    day = TODAY.isoformat()
    fake.routes.update({
        "/usersummary-service/usersummary/daily": httpx.Response(204),
        "/sleep-service/sleep/dailySleepSummariesByDate": [],
        f"/hrv-service/hrv/daily/{day}/{day}": httpx.Response(204),
        "/activitylist-service/activities/search/activities": [],
    })
    assert "No data for this day yet" in commands.handle("today")

    fake.routes.update({
        "/usersummary-service/usersummary/daily": fixture("user_summary_daily"),
        "/sleep-service/sleep/dailySleepSummariesByDate": fixture("daily_sleep"),
        f"/hrv-service/hrv/daily/{day}/{day}": fixture("hrv_daily"),
        "/activitylist-service/activities/search/activities": fixture("activities"),
    })
    runtime.client()._cache.clear()
    text = commands.handle("today")
    assert "Sleep: 7:05 h, score 82 (deep 1:25" in text
    assert text.startswith("Garmin Connect, 2026-11-06 (watch synced today 10:12)\nNote: last sync 5 h ago")
    assert "HRV: 7-day avg 52 ms (Balanced); last night 43 ms, below your usual 45–60 ms" in text
    assert "Body Battery: 64 latest, 78 at wake, range 30–84, +54 charged, −20 drained" in text
    assert "Steps: 4,210 / 8,000 (as of 10:12)" in text
    assert "Last activity: Morning Run (Running), yesterday 07:02, 10.02 km, 52 min" in text
