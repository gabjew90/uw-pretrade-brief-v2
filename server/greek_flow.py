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


def session_date(payload):
    """ET-naive date string of the data (from the newest row's timestamp), or ''."""
    rows = _data(payload)
    if not rows:
        return ""
    ts = rows[-1].get("timestamp") or ""
    return ts[:10] if isinstance(ts, str) else ""


def accumulation_read(cumsum: list[float]) -> tuple[str, float]:
    """Path read on a cumsum curve. efficiency = |final| / max(|cumsum|) (1.0 = clean
    one-way; ≪1 = spiked & reverted). Zero-crossing aware:
      reversed = the curve crossed to the OPPOSITE side of its peak (flipped sign),
      fading   = reverted toward zero but stayed the same side (eff ≤ 0.4),
      building = eff ≥ 0.7, choppy = the 0.4–0.7 middle, flat = no movement."""
    if not cumsum:
        return "flat", 0.0
    final = cumsum[-1]
    peak = max(cumsum, key=abs)          # signed value at max |.|
    peak_abs = abs(peak)
    if peak_abs == 0:
        return "flat", 0.0
    eff = abs(final) / peak_abs
    crossed = (peak > 0 and final < 0) or (peak < 0 and final > 0)
    if crossed:
        state = "reversed"
    elif eff >= 0.7:
        state = "building"
    elif eff <= 0.4:
        state = "fading"
    else:
        state = "choppy"
    return state, round(eff, 2)


def _downsample(xs: list[float], n: int = 48) -> list[float]:
    """Even ≤n-point sample of a curve for a compact sparkline (keeps the last point)."""
    if len(xs) <= n:
        return [round(x) for x in xs]
    step = len(xs) / n
    out = [round(xs[min(len(xs) - 1, int(i * step))]) for i in range(n)]
    out[-1] = round(xs[-1])
    return out


def build_composite(payload) -> dict:
    """Tile 1's net-delta composite (display-only). LEAD = dir_delta_flow (the
    directional single-leg bets — the strategy-relevant read); total_delta_flow is
    the 'all-flow incl. hedges' cross-read. The sparkline + accumulation path follow
    the LED (directional) curve so the headline and the curve agree. Returns
    available=False when the directional curve is degenerate (→ 'unavailable', never
    a silent 'flat').

    NB sign convention: the 6/5 event anchor pins total_delta_flow's sign
    (negative=bearish) but dir_delta_flow ran the OTHER way at that minute, so the
    DIRECTIONAL sign is NOT independently validated — the render leads cautiously and
    stands down on divergence rather than asserting a confident directional call."""
    if is_degenerate(payload, "dir_delta_flow"):
        return {"available": False}
    cd_dir = cumdelta(payload, "dir_delta_flow")
    state, eff = accumulation_read(cd_dir)
    return {
        "available": True,
        "dir_delta": round(cd_dir[-1]),                              # directional bets (headline/led)
        "net_delta": round(session_net(payload, "total_delta_flow")),  # all-flow incl. hedges (secondary)
        "accumulation": state,
        "efficiency": eff,
        "cumsum": _downsample(cd_dir, 48),
        "session_date": session_date(payload),
    }
