"""Request governor — the one place that owns the UW budget and rate limits.

UW Basic is the binding constraint: 120/min, daily cap (~15k fallback; UW headers are
authoritative). The governor turns "should I make this call?" into a single typed
decision, with:

  - a per-minute (sliding 60s) + per-day meter, preferring UW response headers,
  - PRIORITY (direction-critical fetches beat nice-to-have context),
  - request COALESCING (same endpoint+params in-flight → one call, many awaiters),
  - persistence of the daily meter (uw_budget.json, atomic temp→replace) so a redeploy
    doesn't reset the daily counter,
  - graceful degradation surfaced through PROVENANCE (a denied call returns a Decision
    the caller records as quality=unavailable, never a silent failure).

No pipeline stage or service makes a live UW call without passing `governor.check()`.
In REPLAY mode every live call is denied (callers must read bronze) — enforced here so
no stage can bypass it.

Time is injectable (`now`) everywhere so the daily-reset and 60s-window logic are
deterministic in tests (v2's lesson). Ported from `e1d6c5e:server/budget.py`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Callable, TypeVar

from server.config import settings

log = logging.getLogger(__name__)

_PERSIST_EVERY = 25           # flush the daily meter to disk at most every N calls
_MINUTE_HEADROOM = 115        # deny at 115 of UW's 120/min (headroom against 429 cascade)

T = TypeVar("T")


class Priority(IntEnum):
    CRITICAL = 0    # direction-deciding fetch (flow, OI for the chosen side)
    NORMAL = 1      # confirming context (gamma, skew, cost)
    LOW = 2         # nice-to-have (news, seasonality, sector tide)


@dataclass
class Decision:
    allow: bool
    reason: str = ""          # why denied (for provenance.note when not allowed)
    spent_today: int = 0
    cap: int = 0


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(tz=timezone.utc)


@dataclass
class _Meter:
    """The thread-safe budget state. Prefers UW's authoritative headers when seen;
    falls back to a local counter + env-var cap until then."""
    cap_day: int
    soft_pct: float
    day_key: str | None = None
    day_count: int = 0                                   # local fallback counter
    minute: deque = field(default_factory=deque)         # datetimes within the rolling 60s
    uw_daily_count: int | None = None                    # header-sourced (authoritative)
    uw_daily_limit: int | None = None
    uw_minute_remaining: int | None = None
    persist_dirty: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _roll(self, now: datetime) -> None:
        """Prune the 60s window and reset the daily bucket on a UTC-date change.
        Caller holds _lock."""
        cutoff = now.timestamp() - 60
        while self.minute and self.minute[0].timestamp() <= cutoff:
            self.minute.popleft()
        key = now.date().isoformat()
        if key != self.day_key:
            self.day_key = key
            self.day_count = 0
            self.uw_daily_count = None     # next UW response re-seeds the new day
            self.uw_daily_limit = None
            self.uw_minute_remaining = None

    def _count_cap_source(self) -> tuple[int, int, str]:
        """Effective (count, cap, source) — UW headers win when seen. Caller holds _lock."""
        if self.uw_daily_count is not None:
            return self.uw_daily_count, (self.uw_daily_limit or self.cap_day), "uw_headers"
        return self.day_count, self.cap_day, "local"

    def per_minute(self) -> int:
        return len(self.minute)


class Governor:
    def __init__(self) -> None:
        self.meter = _Meter(cap_day=settings.uw_daily_cap, soft_pct=settings.uw_budget_soft_pct)
        self.replay = settings.replay
        self._inflight: dict[str, tuple[threading.Event, list]] = {}
        self._inflight_lock = threading.Lock()

    # ── budget decisions ────────────────────────────────────────────────────
    def check(self, *, priority: Priority = Priority.NORMAL, now: datetime | None = None) -> Decision:
        if self.replay:
            return Decision(False, "replay: read bronze, no live UW")
        n = _now(now)
        m = self.meter
        with m._lock:
            m._roll(n)
            count, cap, _ = m._count_cap_source()
            if count >= cap:
                return Decision(False, "daily cap reached", count, cap)
            if m.per_minute() >= _MINUTE_HEADROOM:
                return Decision(False, "per-minute limit", count, cap)
            if count >= int(cap * m.soft_pct) and priority > Priority.CRITICAL:
                return Decision(False, "over soft cap — critical only", count, cap)
            return Decision(True, "", count, cap)

    def record(self, n: int = 1, *, now: datetime | None = None) -> None:
        """Count UW HTTP attempts (once per request, including retries)."""
        t = _now(now)
        flush = False
        with self.meter._lock:
            self.meter._roll(t)
            for _ in range(n):
                self.meter.minute.append(t)
            self.meter.day_count += n
            self.meter.persist_dirty += n
            if self.meter.persist_dirty >= _PERSIST_EVERY:
                self.meter.persist_dirty = 0
                flush = True
        if flush:
            self._persist()

    def update_from_headers(self, headers: Any, *, now: datetime | None = None) -> None:
        """Capture UW's authoritative usage from a successful response's headers
        (any case-insensitive mapping). Header values override the local counter."""
        t = _now(now)
        with self.meter._lock:
            self.meter._roll(t)
            c = _int(headers, "x-uw-daily-req-count")
            lim = _int(headers, "x-uw-token-req-limit")
            rem = _int(headers, "x-uw-req-per-minute-remaining")
            if c is not None:
                self.meter.uw_daily_count = c
            if lim is not None:
                self.meter.uw_daily_limit = lim
            if rem is not None:
                self.meter.uw_minute_remaining = rem

    def snapshot(self, *, now: datetime | None = None) -> dict:
        t = _now(now)
        with self.meter._lock:
            self.meter._roll(t)
            count, cap, source = self.meter._count_cap_source()
            return {
                "calls_1m": self.meter.per_minute(),
                "calls_today": count,
                "daily_cap": cap,
                "budget_pct": round(count / cap * 100, 1) if cap else 0.0,
                "minute_remaining": self.meter.uw_minute_remaining,
                "source": source,
                "day": self.meter.day_key,
            }

    # ── request coalescing ──────────────────────────────────────────────────
    def coalesce(self, key: str, producer: Callable[[], T]) -> T:
        """Run `producer` once per in-flight `key`; concurrent callers with the same key
        wait for the leader and share its result (or its exception). Keyed on
        (endpoint, params) by the caller. Process-wide (single-worker, personal use)."""
        with self._inflight_lock:
            entry = self._inflight.get(key)
            if entry is None:
                event = threading.Event()
                box: list = []          # [("ok", value)] or [("err", exc)]
                self._inflight[key] = (event, box)
                leader = True
            else:
                event, box = entry
                leader = False
        if not leader:
            event.wait()
            kind, payload = box[0]
            if kind == "err":
                raise payload
            return payload
        try:
            value = producer()
            box.append(("ok", value))
            return value
        except BaseException as e:   # share the failure with all waiters
            box.append(("err", e))
            raise
        finally:
            with self._inflight_lock:
                self._inflight.pop(key, None)
            event.set()

    # ── persistence ─────────────────────────────────────────────────────────
    def _persist_path(self):
        return settings.data_dir / "uw_budget.json"

    def _persist(self) -> None:
        """Best-effort atomic flush of {day, count}. Never raises."""
        try:
            with self.meter._lock:
                payload = {"day": self.meter.day_key, "count": self.meter.day_count}
            p = self._persist_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, p)
        except Exception as e:   # pragma: no cover - persistence is best-effort
            log.debug("governor persist failed (non-fatal): %s", e)

    def load_persisted(self, *, now: datetime | None = None) -> None:
        """Restore today's count on boot so a redeploy doesn't reset the daily meter.
        A persisted count from a previous UTC day is ignored."""
        t = _now(now)
        try:
            data = json.loads(self._persist_path().read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("day") == t.date().isoformat():
            with self.meter._lock:
                self.meter.day_key = data["day"]
                self.meter.day_count = int(data.get("count") or 0)

    def reset(self) -> None:
        """Clear all IN-MEMORY state (does not touch the persisted file). Test support /
        cold-boot simulation before load_persisted()."""
        with self.meter._lock:
            self.meter.minute.clear()
            self.meter.day_key = None
            self.meter.day_count = 0
            self.meter.persist_dirty = 0
            self.meter.uw_daily_count = None
            self.meter.uw_daily_limit = None
            self.meter.uw_minute_remaining = None
        with self._inflight_lock:
            self._inflight.clear()


def _int(headers: Any, name: str) -> int | None:
    try:
        v = headers.get(name)
        return int(v) if v is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


governor = Governor()
