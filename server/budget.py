"""Process-local UW API call meter + soft budget guard.

UW Basic is 120 req/min · 40k req/day. The 2026-05-29 outage had no visibility
into consumption — the first signal was a 429 cascade. This meter tracks calls
so we can (a) surface usage on /health and (b) shed non-critical load *before*
the hard cap. Thread-safe: UW calls run across ThreadPoolExecutor workers.

`now` is injectable on every function for deterministic tests; production callers
omit it and get the current UTC time.
"""
from __future__ import annotations
import os
import threading
from collections import deque
from datetime import datetime, timezone

_lock = threading.Lock()
_minute: deque[datetime] = deque()   # call timestamps within the rolling 60s window
_day_key: str | None = None          # UTC date of the current daily bucket
_day_count = 0


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(tz=timezone.utc)


def _daily_cap() -> int:
    try:
        return int(os.environ.get("UW_DAILY_CAP", "40000"))
    except ValueError:
        return 40000


def _soft_pct() -> float:
    try:
        return float(os.environ.get("UW_BUDGET_SOFT_PCT", "0.9"))
    except ValueError:
        return 0.9


def _roll(now: datetime) -> None:
    """Prune the 60s window and reset the daily bucket on a UTC-date change.
    Caller must hold _lock."""
    global _day_key, _day_count
    cutoff = now.timestamp() - 60   # keep only calls strictly within the last 60s
    while _minute and _minute[0].timestamp() <= cutoff:
        _minute.popleft()
    key = now.date().isoformat()
    if key != _day_key:
        _day_key = key
        _day_count = 0


def record_call(now: datetime | None = None) -> None:
    """Count one UW HTTP attempt (call once per request, including retries)."""
    now = _now(now)
    with _lock:
        _roll(now)
        _minute.append(now)
        global _day_count
        _day_count += 1


def snapshot(now: datetime | None = None) -> dict:
    now = _now(now)
    with _lock:
        _roll(now)
        cap = _daily_cap()
        return {
            "calls_1m": len(_minute),
            "calls_today": _day_count,
            "daily_cap": cap,
            "budget_pct": round(_day_count / cap * 100, 1) if cap else 0.0,
            "day": _day_key,
        }


def over_soft_budget(now: datetime | None = None) -> bool:
    """True once today's calls reach daily_cap * soft_pct — the signal to shed
    non-critical fetches before UW starts returning 429s."""
    now = _now(now)
    with _lock:
        _roll(now)
        return _day_count >= _daily_cap() * _soft_pct()


def reset() -> None:
    """Clear all counters. Test-support / cold-boot hygiene."""
    global _day_key, _day_count
    with _lock:
        _minute.clear()
        _day_key = None
        _day_count = 0
