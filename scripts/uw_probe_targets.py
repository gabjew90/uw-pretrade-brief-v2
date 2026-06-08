"""Single source of truth for the UW endpoints the v3 pipeline probes + captures.

Both `probe_endpoints.py` (health/sanity) and `capture_golden.py` (golden bronze)
import `build_targets()` so paths/params can never drift between them.

v3 path rule: `uw_client._BASE` already ends in `/api`, so paths here OMIT `/api`
(e.g. `/option-trades/flow-alerts`, not `/api/option-trades/flow-alerts`). All paths
are HYPHENATED — `uw_client.assert_hyphenated()` enforces this at call time.

Paths/params ported from `e1d6c5e:server/uw.py`. flow-alerts is the single endpoint
for both cross-ticker and per-ticker (via `ticker_symbol`); the per-ticker
`/stock/{t}/flow-alerts` route is DEPRECATED. limit caps at 500 (asking more silently
falls back to 50).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

FLOW_ALERTS_MAX = 500


def unwrap_rows(payload) -> list:
    """UW responses are usually {"data": [...]} or a bare list. Normalize to a list."""
    if isinstance(payload, dict):
        d = payload.get("data", payload)
        return d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
    return payload if isinstance(payload, list) else []


def resolve_expiries(ticker: str, get) -> tuple[str, str]:
    """A near-term and a ~30d expiry that actually EXIST for `ticker`, from
    greek-exposure/expiry; falls back to computed Fridays. `get` is uw_client.get."""
    try:
        rows = unwrap_rows(get(f"/stock/{ticker.upper()}/greek-exposure/expiry"))
        exps = sorted({r.get("expiry") for r in rows if r.get("expiry")})
        if exps:
            d0 = datetime.now(tz=timezone.utc).date()
            far = next((e for e in exps
                        if (datetime.fromisoformat(e).date() - d0).days >= 25), exps[-1])
            return exps[0], far
    except Exception:
        pass
    today = datetime.now(tz=timezone.utc).date()
    nf = today + timedelta(days=(4 - today.weekday()) % 7)   # next Friday
    return nf.isoformat(), (nf + timedelta(days=28)).isoformat()


@dataclass
class Target:
    label: str                                   # fixture/key name, e.g. "flow-alerts"
    path: str                                    # v3 UW path (no /api prefix)
    params: dict = field(default_factory=dict)
    required_keys: list[str] = field(default_factory=list)   # keys parsers will read
    sanity: Optional[Callable[[list], Optional[str]]] = None  # -> warning str or None
    critical: bool = False                       # Phase-3 / cross-check load-bearing


# ── value-sanity invariants (return a warning string, or None if OK) ──────────
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _sane_flow(rows: list) -> Optional[str]:
    if not rows:
        return None
    # opening-flow proxy: v2 found all_opening_trades dead → direction uses volume_oi_ratio
    has_voi = any(r.get("volume_oi_ratio") is not None for r in rows)
    if not has_voi:
        return "no volume_oi_ratio on any row (opening-flow direction has no input!)"
    return None


def _sane_greek_flow(rows: list) -> Optional[str]:
    if not rows:
        return None
    if rows[0].get("dir_delta_flow") is None:
        return "no dir_delta_flow (conviction signal has no input)"
    mx = max((abs(_f(r.get("dir_delta_flow")) or 0.0) for r in rows), default=0.0)
    if mx == 0.0:
        return "dir_delta_flow uniformly 0 across the series (degenerate)"
    return None


def _sane_net_prem(rows: list) -> Optional[str]:
    if not rows:
        return None
    need = ("net_call_premium", "net_put_premium", "net_delta")
    miss = [k for k in need if all(r.get(k) is None for r in rows)]
    if miss:
        return f"net-prem-ticks fields never populated: {miss}"
    mx = max((abs(_f(r.get("net_delta")) or 0.0) for r in rows), default=0.0)
    if mx == 0.0:
        return "net_delta uniformly 0 across the series (degenerate)"
    return None


def _sane_oi(rows: list) -> Optional[str]:
    if not rows:
        return None
    miss = [k for k in ("strike", "call_oi", "put_oi") if rows[0].get(k) is None]
    return f"oi-per-strike missing keys: {miss}" if miss else None


def _sane_rr(rows: list, ticker: str = "") -> Optional[str]:
    if not rows:
        return None
    rr = _f(rows[-1].get("risk_reversal"))
    if rr is None:
        return "latest risk_reversal missing/non-numeric"
    # vendor risk_reversal = put_IV - call_IV (positive = put-skew); an index is
    # structurally put-skewed → expect positive. Flag a possible sign-convention flip.
    if ticker.upper() in ("SPY", "QQQ", "IWM", "DIA") and rr <= 0:
        return f"index {ticker} risk_reversal={rr} not put-skewed (sign may have flipped!)"
    return None


def build_targets(ticker: str, near: str, far: str) -> list[Target]:
    t = ticker.upper()
    return [
        # ── Phase-3 critical + cross-checks ──────────────────────────────────
        Target("flow-alerts", "/option-trades/flow-alerts",
               {"ticker_symbol": t, "limit": FLOW_ALERTS_MAX},
               ["ticker", "type", "total_premium", "volume_oi_ratio", "created_at"],
               _sane_flow, critical=True),
        Target("greek-flow", f"/stock/{t}/greek-flow", {},
               ["timestamp", "dir_delta_flow", "total_delta_flow"],
               _sane_greek_flow, critical=True),
        Target("net-prem-ticks", f"/stock/{t}/net-prem-ticks", {},
               ["net_call_premium", "net_put_premium", "net_delta"],
               _sane_net_prem, critical=True),
        Target("oi-per-strike", f"/stock/{t}/oi-per-strike", {},
               ["strike", "call_oi", "put_oi"], _sane_oi, critical=True),
        # ── Phase-4 signals (captured now for completeness) ──────────────────
        Target("spot-exposures-strike", f"/stock/{t}/spot-exposures/strike", {},
               ["strike"], None),
        Target("greek-exposure-expiry", f"/stock/{t}/greek-exposure/expiry", {},
               ["expiry", "dte"], None),
        Target("historical-risk-reversal-skew", f"/stock/{t}/historical-risk-reversal-skew",
               {"expiry": far, "delta": 25}, ["date", "risk_reversal"],
               lambda rows: _sane_rr(rows, t)),
        Target("greeks", f"/stock/{t}/greeks", {"expiry": far},
               ["call_delta", "put_delta", "call_volatility", "put_volatility"], None),
        Target("volatility-term-structure", f"/stock/{t}/volatility/term-structure", {},
               ["expiry", "volatility"], None),
        Target("interpolated-iv", f"/stock/{t}/interpolated-iv", {},
               ["days", "volatility", "percentile"], None),
        Target("volatility-realized", f"/stock/{t}/volatility/realized", {},
               ["implied_volatility", "realized_volatility"], None),
        Target("earnings", f"/stock/{t}/earnings", {}, [], None),
        Target("stock-state", f"/stock/{t}/stock-state", {}, [], None),
        # ── market-wide (regime) ─────────────────────────────────────────────
        Target("market-tide", "/market/market-tide", {}, [], None),
        Target("economic-calendar", "/market/economic-calendar", {}, ["event", "time"], None),
    ]
