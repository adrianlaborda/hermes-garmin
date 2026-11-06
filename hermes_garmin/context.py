"""``pre_llm_call`` hook: a one-line Garmin status, once per session and day, when enabled.

The hook runs inside the turn, so it must never block it: the fetch runs on a worker thread with a
hard time budget and no retries. A fetch that misses the budget keeps running and warms the cache,
so the next session of the day gets the line instantly. Any failure just skips the injection.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from datetime import date
from typing import Any, Optional

from . import reports
from .runtime import RUNTIME, Runtime

logger = logging.getLogger(__name__)

BUDGET_S = 3.0
LINE_TTL_S = 1800.0
FAILURE_BACKOFF_S = 600.0


class DailyContext:
    def __init__(self, runtime: Runtime = RUNTIME, *, budget: float = BUDGET_S,
                 clock=time.monotonic) -> None:
        self._runtime = runtime
        self._budget = budget
        self._clock = clock
        self._lock = threading.Lock()
        self._done: set[tuple[str, str]] = set()  # (session, day) already injected
        self._lines: dict[tuple[str, str], tuple[float, str]] = {}  # (key fingerprint, day) -> line
        self._failed_at: Optional[float] = None
        self._inflight: Optional[threading.Thread] = None

    def __call__(self, session_id: str = "", parent_session_id: str = "", **_kwargs: Any) -> Optional[dict]:
        if parent_session_id:  # subagents get their task from the parent, not the user's day
            return None
        try:
            return self._context(str(session_id or ""))
        except Exception:  # a context hint is never worth breaking a turn
            logger.debug("garmin context skipped", exc_info=True)
            return None

    def _context(self, session_id: str) -> Optional[dict]:
        runtime = self._runtime
        if not runtime.flag("inject_daily_context", False) or not runtime.has_key():
            return None
        day = runtime.today().isoformat()
        if (session_id, day) in self._done:
            return None
        client = runtime.client()
        cache_key = (client.fingerprint(), day)
        line = self._cached(cache_key)
        if line is None:
            line = self._fetch(cache_key, day)
        if not line:
            return None
        with self._lock:
            self._done = {done for done in self._done if done[1] == day}
            self._done.add((session_id, day))
        return {"context": line}

    def _cached(self, cache_key: tuple) -> Optional[str]:
        with self._lock:
            hit = self._lines.get(cache_key)
        if hit and self._clock() - hit[0] < LINE_TTL_S:
            return hit[1]
        return None

    def _fetch(self, cache_key: tuple, day: str) -> Optional[str]:
        with self._lock:
            if self._failed_at is not None and self._clock() - self._failed_at < FAILURE_BACKOFF_S:
                return None
            worker = self._inflight
            if worker is None or not worker.is_alive():
                # A copied context keeps Hermes' per-profile secret scope visible to the worker.
                worker = self._inflight = threading.Thread(
                    target=contextvars.copy_context().run, args=(self._work, cache_key, day),
                    name="garmin-context", daemon=True)
                worker.start()
        worker.join(self._budget)
        return self._cached(cache_key)

    def _work(self, cache_key: tuple, day: str) -> None:
        try:
            report = reports.today(self._runtime.client(), date.fromisoformat(day), retries=0)
            line = reports.status_line(report)
        except Exception as exc:
            logger.debug("garmin context fetch failed: %s", exc)
            with self._lock:
                self._failed_at = self._clock()
            return
        with self._lock:
            self._lines[cache_key] = (self._clock(), line)
            self._failed_at = None
