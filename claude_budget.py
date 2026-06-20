#!/usr/bin/env python3
"""Shared cross-process daily Claude-call budget.

Both ``api.py`` (in-process chat endpoints) and ``main.py`` (scan / score-more,
which run as subprocesses) import this module so that *every* Claude API call —
no matter which process makes it — counts against a single
``DAILY_CLAUDE_CALL_LIMIT`` ceiling.

The counter is persisted to ``claude_calls.json`` (a list of wall-clock
timestamps in a 24-hour sliding window) so it survives restarts and is shared
across processes. Read-modify-write is guarded by an exclusive ``fcntl`` file
lock, so concurrent access from a running scan subprocess and a chat request
cannot race.

Usage:
    claude_budget.check()    # raises ClaudeBudgetExceeded if the window is full
    <make the Claude API call>
    claude_budget.record()   # +1 only after a successful call
"""

import fcntl
import json
import os
import time
from pathlib import Path
from typing import List, Tuple

_STORE = Path(__file__).parent / "claude_calls.json"
_WINDOW = 86400  # 24-hour sliding window, in seconds
_LIMIT = int(os.environ.get("DAILY_CLAUDE_CALL_LIMIT", "80"))


class ClaudeBudgetExceeded(Exception):
    """Raised when the daily Claude-call budget is exhausted."""


def _with_lock(mutate) -> object:
    """Open the store under an exclusive lock, prune the window, run ``mutate``.

    ``mutate(timestamps, now)`` must return ``(result, new_timestamps)``. The
    pruned-and-mutated list is written back atomically while the lock is held.
    """
    _STORE.touch(exist_ok=True)
    with open(_STORE, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            raw = f.read().strip()
            try:
                stamps: List[float] = json.loads(raw) if raw else []
            except (json.JSONDecodeError, ValueError):
                stamps = []
            if not isinstance(stamps, list):
                stamps = []
            now = time.time()
            stamps = [t for t in stamps if isinstance(t, (int, float)) and now - t <= _WINDOW]
            result, stamps = mutate(stamps, now)
            f.seek(0)
            f.truncate()
            json.dump(stamps, f)
            return result
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def check() -> None:
    """Raise :class:`ClaudeBudgetExceeded` if the 24h window is already full.

    Read-only with respect to the count (it only prunes expired timestamps).
    Call this immediately before every Claude API call.
    """
    def _mutate(stamps: List[float], now: float) -> Tuple[None, List[float]]:
        if len(stamps) >= _LIMIT:
            raise ClaudeBudgetExceeded(
                f"Daily Claude-call limit of {_LIMIT} reached."
            )
        return None, stamps

    _with_lock(_mutate)


def record() -> None:
    """Record one successful Claude call against the budget."""
    def _mutate(stamps: List[float], now: float) -> Tuple[None, List[float]]:
        stamps.append(now)
        return None, stamps

    _with_lock(_mutate)


def remaining() -> int:
    """Return how many calls are still available in the current window."""
    def _mutate(stamps: List[float], now: float) -> Tuple[int, List[float]]:
        return max(0, _LIMIT - len(stamps)), stamps

    return _with_lock(_mutate)
