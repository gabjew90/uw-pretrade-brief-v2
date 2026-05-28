"""Tracked universe: who do we archive, how does the sticky list decay.

The composition rule is `pinned ∪ indices ∪ sticky ∪ hot_15`. See spec §3b
for the rationale (selection-bias prevention)."""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable


INDICES: frozenset[str] = frozenset({"SPX", "QQQ", "IWM", "VIX", "GLD"})

# Calendar-day buffer for the 30-trading-day decay window.
# ~30 trading days ≈ 44 calendar days; use 45 to be unambiguous.
_STICKY_DECAY_DAYS = 45


def parse_pinned() -> set[str]:
    """Parse TICKER_PIN_LIST env var → set of uppercased, stripped tickers."""
    raw = os.environ.get("TICKER_PIN_LIST", "")
    if not raw:
        return set()
    return {t.strip().upper() for t in raw.split(",") if t.strip()}


class StickyState:
    """Tickers we keep tracking after they've appeared in hot_15.

    Backed by /data/sticky.json (loaded/saved by server.storage)."""

    def __init__(self, state: dict[str, str]):
        self._state: dict[str, str] = dict(state)

    def touch(self, tickers: Iterable[str], *, now: datetime) -> None:
        ts = now.isoformat()
        for t in tickers:
            self._state[t] = ts

    def active(self, *, now: datetime) -> set[str]:
        cutoff = now - timedelta(days=_STICKY_DECAY_DAYS)
        return {t for t, iso in self._state.items()
                if _safe_parse_iso(iso, fallback=now) >= cutoff}

    def decay(self, *, now: datetime) -> None:
        active = self.active(now=now)
        self._state = {t: ts for t, ts in self._state.items() if t in active}

    def to_dict(self) -> dict[str, str]:
        return dict(self._state)


def _safe_parse_iso(iso: str, fallback: datetime) -> datetime:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return fallback


def compose_universe(
    *,
    hot_15: Iterable[str],
    sticky: StickyState,
    now: datetime,
) -> set[str]:
    """tracked = pinned ∪ indices ∪ sticky.active() ∪ hot_15."""
    return parse_pinned() | set(INDICES) | sticky.active(now=now) | set(hot_15)


def top_15_unique_tickers(flow_alerts_payload: dict) -> list[str]:
    """Extract top-15 unique tickers from /api/option-trades/flow-alerts payload,
    preserving order (UW response is already rank-sorted)."""
    rows = (
        flow_alerts_payload.get("data", flow_alerts_payload)
        if isinstance(flow_alerts_payload, dict)
        else flow_alerts_payload
    )
    seen: list[str] = []
    for r in rows:
        t = r.get("ticker") or r.get("ticker_symbol") or r.get("underlying")
        if t and t not in seen:
            seen.append(t)
        if len(seen) >= 15:
            break
    return seen
