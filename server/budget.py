"""Process-local UW API call meter + soft budget guard.

UW Basic is 120 req/min · 40k req/day. The 2026-05-29 outage had no visibility
into consumption — the first signal was a 429 cascade. This meter tracks calls
so we can (a) surface usage on /health and (b) shed non-critical load *before*
the hard cap. Thread-safe: UW calls run across ThreadPoolExecutor workers.

`now` is injectable on every function for deterministic tests; production callers
omit it and get the current UTC time.
"""
from __future__ import annotations
import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_lock = threading.Lock()
_minute: deque[datetime] = deque()   # call timestamps within the rolling 60s window
_day_key: str | None = None          # UTC date of the current daily bucket
_day_count = 0                       # LOCAL fallback counter (used until UW headers seen)
_persist_dirty = 0                   # calls since last disk flush (throttle)
_PERSIST_EVERY = 25                  # flush at most every N calls

# UW returns the AUTHORITATIVE usage in response headers (x-uw-daily-req-count,
# x-uw-token-req-limit, x-uw-req-per-minute-remaining). When we've seen them we
# prefer them over the local guess — UW's count is the real one, the cap is the
# real one (their docs example is 15000, NOT our assumed 40000), and the daily
# reset is 8PM ET = 00:00 UTC. These reset to None on a new UTC day.
_uw_daily_count: int | None = None
_uw_daily_limit: int | None = None
_uw_minute_remaining: int | None = None


def _persist_path() -> Path:
    # Read DATA_DIR directly (avoid importing storage → circular). Matches
    # storage._data_dir() default of /data.
    return Path(os.environ.get("DATA_DIR", "/data")) / "uw_budget.json"


def _flush_locked() -> None:
    """Persist {day_key, day_count} to the volume. Best-effort — never raises.
    Caller holds _lock."""
    try:
        p = _persist_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"day": _day_key, "count": _day_count}), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        log.debug("budget persist failed (non-fatal): %s", e)


def load_persisted(now: datetime | None = None) -> None:
    """Restore today's count from disk on boot, so a cold-boot/redeploy doesn't
    reset the daily meter to 0 (which let the cap sneak up on 2026-06-01). A
    persisted count from a previous UTC day is ignored."""
    now = _now(now)
    with _lock:
        global _day_key, _day_count
        try:
            data = json.loads(_persist_path().read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("day") == now.date().isoformat():
            _day_key = data["day"]
            _day_count = int(data.get("count") or 0)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(tz=timezone.utc)


def _daily_cap() -> int:
    # FALLBACK only — used until UW's x-uw-token-req-limit header is seen, which
    # is authoritative. Defaulted CONSERVATIVELY to 15000 (UW's docs example
    # cap) rather than the old 40000 guess, so the guard errs toward shedding if
    # we never see a header.
    try:
        return int(os.environ.get("UW_DAILY_CAP", "15000"))
    except ValueError:
        return 15000


def _soft_pct() -> float:
    try:
        return float(os.environ.get("UW_BUDGET_SOFT_PCT", "0.9"))
    except ValueError:
        return 0.9


def _roll(now: datetime) -> None:
    """Prune the 60s window and reset the daily bucket on a UTC-date change.
    Caller must hold _lock."""
    global _day_key, _day_count, _uw_daily_count, _uw_daily_limit, _uw_minute_remaining
    cutoff = now.timestamp() - 60   # keep only calls strictly within the last 60s
    while _minute and _minute[0].timestamp() <= cutoff:
        _minute.popleft()
    key = now.date().isoformat()
    if key != _day_key:
        _day_key = key
        _day_count = 0
        _uw_daily_count = None        # next UW response re-seeds the new day
        _uw_daily_limit = None
        _uw_minute_remaining = None


def _int(headers, name) -> int | None:
    try:
        v = headers.get(name)
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def record_usage_headers(headers, now: datetime | None = None) -> None:
    """Capture UW's authoritative usage from a successful response's headers.
    `headers` is any case-insensitive mapping (requests.Response.headers)."""
    now = _now(now)
    with _lock:
        _roll(now)
        global _uw_daily_count, _uw_daily_limit, _uw_minute_remaining
        c = _int(headers, "x-uw-daily-req-count")
        lim = _int(headers, "x-uw-token-req-limit")
        rem = _int(headers, "x-uw-req-per-minute-remaining")
        if c is not None:
            _uw_daily_count = c
        if lim is not None:
            _uw_daily_limit = lim
        if rem is not None:
            _uw_minute_remaining = rem


def record_call(now: datetime | None = None) -> None:
    """Count one UW HTTP attempt (call once per request, including retries)."""
    now = _now(now)
    with _lock:
        _roll(now)
        _minute.append(now)
        global _day_count, _persist_dirty
        _day_count += 1
        _persist_dirty += 1
        if _persist_dirty >= _PERSIST_EVERY:
            _persist_dirty = 0
            _flush_locked()


def snapshot(now: datetime | None = None) -> dict:
    now = _now(now)
    with _lock:
        _roll(now)
        # Prefer UW's authoritative headers; fall back to the local guess.
        if _uw_daily_count is not None:
            count = _uw_daily_count
            cap = _uw_daily_limit or _daily_cap()
            source = "uw_headers"
        else:
            count = _day_count
            cap = _daily_cap()
            source = "local"
        return {
            "calls_1m": len(_minute),
            "calls_today": count,
            "daily_cap": cap,
            "budget_pct": round(count / cap * 100, 1) if cap else 0.0,
            "minute_remaining": _uw_minute_remaining,
            "source": source,
            "day": _day_key,
        }


def over_soft_budget(now: datetime | None = None) -> bool:
    """True once today's calls reach daily_cap * soft_pct — the signal to shed
    non-critical fetches before UW starts returning 429s. Uses UW's real count +
    cap when we've seen the headers, else the local guess."""
    now = _now(now)
    with _lock:
        _roll(now)
        if _uw_daily_count is not None:
            count = _uw_daily_count
            cap = _uw_daily_limit or _daily_cap()
        else:
            count = _day_count
            cap = _daily_cap()
        return count >= cap * _soft_pct()


def reset() -> None:
    """Clear all IN-MEMORY counters (does not touch the persisted file).
    Test-support / simulating a cold-boot before load_persisted()."""
    global _day_key, _day_count, _persist_dirty
    global _uw_daily_count, _uw_daily_limit, _uw_minute_remaining
    with _lock:
        _minute.clear()
        _day_key = None
        _day_count = 0
        _persist_dirty = 0
        _uw_daily_count = None
        _uw_daily_limit = None
        _uw_minute_remaining = None
