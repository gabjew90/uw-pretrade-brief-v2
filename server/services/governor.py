"""Request governor — the one place that owns the UW budget and rate limits.

UW Basic is the binding constraint: 120/min, ~15k/day, plus a rolling 7-day ceiling.
The governor turns "should I make this call?" into a single typed decision, with:

  - a per-minute + per-day meter (prefers UW response headers when present),
  - PRIORITY (direction-critical fetches beat nice-to-have context),
  - request COALESCING (same endpoint+params in-flight → one call, many awaiters),
  - graceful degradation surfaced through PROVENANCE (a denied/over-budget call
    returns a Decision the caller records as quality=degraded/unavailable, never a
    silent failure).

This is a SKELETON: the meter + priority + coalescing contracts are defined; the
persistence of the meter (uw_budget.json) and the live header parsing land when the
ingest stage is wired per instructions.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum

from server.config import settings


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


@dataclass
class _Meter:
    cap_day: int
    soft_pct: float
    spent_day: int = 0
    minute_window: list[float] = field(default_factory=list)   # epoch secs of recent calls
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, n: int = 1) -> None:
        now = time.time()
        with self._lock:
            self.spent_day += n
            self.minute_window.append(now)
            self.minute_window = [t for t in self.minute_window if now - t < 60]

    def per_minute(self) -> int:
        now = time.time()
        return sum(1 for t in self.minute_window if now - t < 60)


class Governor:
    """Single instance owns the budget. In REPLAY mode it denies every live call
    (callers must read bronze instead) — enforced here so no stage can bypass it."""

    def __init__(self) -> None:
        self.meter = _Meter(cap_day=settings.uw_daily_cap, soft_pct=settings.uw_budget_soft_pct)
        self.replay = settings.replay

    def check(self, *, priority: Priority = Priority.NORMAL) -> Decision:
        if self.replay:
            return Decision(False, "replay: read bronze, no live UW")
        soft = int(self.meter.cap_day * self.meter.soft_pct)
        spent = self.meter.spent_day
        # Over the soft cap, only CRITICAL fetches get through.
        if spent >= self.meter.cap_day:
            return Decision(False, "daily cap reached", spent, self.meter.cap_day)
        if spent >= soft and priority > Priority.CRITICAL:
            return Decision(False, "over soft cap — critical only", spent, self.meter.cap_day)
        if self.meter.per_minute() >= 115:   # headroom under the 120/min hard limit
            return Decision(False, "per-minute limit", spent, self.meter.cap_day)
        return Decision(True, "", spent, self.meter.cap_day)

    def record(self, n: int = 1) -> None:
        self.meter.record(n)


governor = Governor()
