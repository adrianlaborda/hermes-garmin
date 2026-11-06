"""Read-only backend for the Hermes Desktop panel, mounted at ``/api/plugins/garmin-connect/``.

Hermes imports this file on its own (only when ``garmin-connect`` is in ``plugins.enabled``), so the
sibling ``hermes_garmin`` package is loaded by path under a private module name. Every route
answers 200 with a ``state`` (``ok``, ``not_configured``, ``auth_error``, ``rate_limited``,
``error``) and never returns the API key or the access token.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter

# Why sys.modules: Hermes imports this file standalone (no parent package, so no relative import),
# and the package's own relative imports (``from .client import ...``) need it registered under some
# name. It is registered under a private name derived from this checkout's path, never a Hermes
# module name, and nothing in Hermes is replaced; sys.path is not touched. This is the same loader
# the official plugins use for their dashboard API (hermes-official-plugins:
# hermes-security/dashboard/plugin_api.py; memory-wiki does the same for one module).
_PACKAGE_PATH = Path(__file__).resolve().parents[1] / "hermes_garmin"
_PACKAGE_NAME = "_garmin_connect_panel_" + hashlib.sha256(str(_PACKAGE_PATH).encode()).hexdigest()[:12]


def _load_package():
    if _PACKAGE_NAME not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _PACKAGE_NAME, _PACKAGE_PATH / "__init__.py", submodule_search_locations=[str(_PACKAGE_PATH)])
        if spec is None or spec.loader is None:
            raise ImportError("hermes_garmin package not found next to the dashboard folder")
        package = importlib.util.module_from_spec(spec)
        sys.modules[_PACKAGE_NAME] = package
        try:
            spec.loader.exec_module(package)
        except Exception:
            sys.modules.pop(_PACKAGE_NAME, None)
            raise
    return (importlib.import_module(".panel", _PACKAGE_NAME),
            importlib.import_module(".runtime", _PACKAGE_NAME))


panel, _runtime = _load_package()
# Settings come from config.yaml on every call; the key from Hermes' secret route on every request.
RUNTIME = _runtime.Runtime(get_config=_runtime.hermes_plugin_setting)

router = APIRouter()


def _flag(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# Plain ``def`` handlers: FastAPI runs them in a worker thread with the request's profile scope.
@router.get("/status")
def status(check: str = "false") -> dict:
    """Key configured, key expiry if known, and with ``check=true`` a live token exchange."""
    return panel.status(RUNTIME, check=_flag(check))


@router.get("/today")
def today() -> dict:
    """Today's overview: sleep, HRV, Body Battery, stress, steps, resting HR, last activity."""
    return panel.today(RUNTIME)


@router.get("/trends")
def trends(days: str = "28", series: str = "true") -> dict:
    """Baselines, flags as short signals and, by default, the daily series behind them."""
    return panel.trend(RUNTIME, days, include_series=_flag(series))


@router.get("/series")
def series(metric: str = "hrv", days: str = "28") -> dict:
    """Daily values of one metric (hrv, rhr, sleep, sleep_score, stress, body_battery, steps, intensity)."""
    return panel.series(RUNTIME, metric, days)


@router.get("/overview")
def overview(days: str = "28", fresh: str = "false") -> dict:
    """Everything the panel shows in one request; ``fresh=true`` bypasses the short response cache."""
    return panel.overview(RUNTIME, days, fresh=_flag(fresh))
