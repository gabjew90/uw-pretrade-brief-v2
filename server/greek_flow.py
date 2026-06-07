"""Pure helpers for the Tile 1 greek-flow delta composite (spec:
docs/superpowers/specs/2026-06-06-tile1-greek-flow-delta-design.md). NO I/O —
callers pass the already-fetched /api/stock/{t}/greek-flow payload.

Probed shape (per-minute rows, decimal strings):
  timestamp, dir_delta_flow, total_delta_flow, dir_vega_flow, total_vega_flow, ...
The fields are PER-MINUTE — the daily figure is sum(...), the accumulation curve is
cumsum(...). Sign convention (pinned via the 6/5 golden fixture, see tests): a
known-bearish minute (heavy ask-side put buying) yields NEGATIVE total_delta_flow,
so positive = net long delta (bullish), negative = net short (bearish)."""
from __future__ import annotations


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _data(payload) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    return [r for r in (payload.get("data") or []) if isinstance(r, dict)]


def series(payload, field: str = "dir_delta_flow") -> list[float]:
    """Per-minute values for `field`, oldest→newest, unparseable rows dropped."""
    out = []
    for r in _data(payload):
        v = _f(r.get(field))
        if v is not None:
            out.append(v)
    return out


def cumdelta(payload, field: str = "dir_delta_flow") -> list[float]:
    """Running cumulative sum of the per-minute series (the accumulation curve)."""
    total = 0.0
    out = []
    for v in series(payload, field):
        total += v
        out.append(total)
    return out


def session_net(payload, field: str = "dir_delta_flow") -> float:
    """The session's net (= last cumsum = sum of per-minute). 0.0 when empty."""
    cd = cumdelta(payload, field)
    return cd[-1] if cd else 0.0


def value_at_minute(payload, hhmm_utc: str, field: str = "dir_delta_flow"):
    """The `field` value at the row whose timestamp is HH:MM (UTC). None if absent."""
    for r in _data(payload):
        ts = r.get("timestamp") or ""
        if isinstance(ts, str) and ts[11:16] == hhmm_utc:
            return _f(r.get(field))
    return None


def is_degenerate(payload, field: str = "dir_delta_flow") -> bool:
    """True when the curve carries no usable signal — empty, or flat/all-equal (incl.
    all-zero). Such a payload must render 'unavailable', never a silent 'flat'."""
    s = series(payload, field)
    if len(s) < 2:
        return True
    return max(s) == min(s)
