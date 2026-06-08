"""Pipeline orchestrator — runs the five stages in order for one ticker.

The thin coordinator the HTTP layer calls: ingest → normalize → derive → decide → present.
It owns NO signal math (each stage does its own job); it sequences the typed boundaries,
assembles the canonical map every signal reads, and handles honest-degrade when a fetch or
parse fails (a missing endpoint → that signal `unavailable`, never a crash, never a guess).

Fetch policy (operator: full fetch, governor-gated): direction-critical flow is CRITICAL;
confirming context is NORMAL; market-wide regime context is LOW — so under budget pressure
the governor sheds the nice-to-haves first and the direction read still lands.

The pure helpers (`_premium_side`, `_flow_cluster`, `_tide_lean`, `_days_to_earnings`,
`_regime_inputs`) carry the cross-signal plumbing and are unit-tested offline; only the
live multi-fetch in `build_canon` needs the network. `assemble_from_canon` is pure given a
canon (REPLAY-reproducible).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from server.models import ViewModel
from server.pipeline.decide import decide
from server.pipeline.derive import _next_high_impact_event, derive_all
from server.pipeline.ingest import RawRecord, ingest
from server.pipeline.normalize import NormalizeError, normalize
from server.pipeline.present import present
from server.services import clock
from server.services.governor import Priority
from server.services.uw_client import UWError

_FLOW_ALERTS = "/option-trades/flow-alerts"
_NEAR_DTE = 14            # flow cluster = near-dated strikes (tile2-confirmation-principle)
_CLUSTER_TOP_N = 5
_OI_HISTORY_SESSIONS = 4  # settled sessions of OI to trend (Phase-2: ~7 available before 403)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _rows(payload) -> list:
    if isinstance(payload, dict):
        d = payload.get("data", payload)
        return d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
    return payload if isinstance(payload, list) else []


def _fetch_norm(endpoint: str, params: dict, ticker: str, priority: Priority) -> list:
    """Ingest + normalize one endpoint; [] on any fetch/parse failure (honest-degrade)."""
    try:
        return normalize(ingest(endpoint, params, ticker=ticker, priority=priority))
    except (UWError, NormalizeError):
        return []


def _fetch_raw(endpoint: str, ticker: str | None, priority: Priority) -> list:
    """Ingest one endpoint and return its raw rows (for inputs with no canonical model
    yet — market-tide, economic-calendar). [] on failure."""
    try:
        return _rows(ingest(endpoint, {}, ticker=ticker, priority=priority).payload)
    except UWError:
        return []


# ── pure cross-signal helpers (unit-tested offline) ───────────────────────────
def _premium_side(flow_alerts) -> str | None:
    c = sum((a.total_premium or 0.0) for a in flow_alerts if a.type == "call")
    p = sum((a.total_premium or 0.0) for a in flow_alerts if a.type == "put")
    if not c and not p:
        return None
    return "call" if c >= p else "put"


def _flow_cluster(flow_alerts, side: str, asof_d: date,
                  top_n: int = _CLUSTER_TOP_N, near: int = _NEAR_DTE) -> list[float]:
    """The near-dated (≤`near` DTE) strikes on `side` with the most premium — the cluster
    positioning anchors OI to (aggregate call+put OI is the trap; tile2 principle)."""
    def dte(a) -> int:
        try:
            return (date.fromisoformat(a.expiry) - asof_d).days
        except (TypeError, ValueError):
            return 999
    by_strike: dict[float, float] = {}
    for a in flow_alerts:
        if a.type == side and a.strike is not None and 0 <= dte(a) <= near:
            by_strike[a.strike] = by_strike.get(a.strike, 0.0) + (a.total_premium or 0.0)
    return sorted(by_strike, key=lambda k: by_strike[k], reverse=True)[:top_n]


def _days_to_earnings(earnings_rows, now: datetime) -> int | None:
    """Days until the next future earnings date, or None (no/unknown earnings — e.g. an
    ETF). Tolerant of the field name since we lack a non-empty golden fixture to pin it."""
    best = None
    for r in earnings_rows or []:
        for k in ("report_date", "date", "next_earnings_date", "earnings_date"):
            v = r.get(k) if isinstance(r, dict) else None
            try:
                d = date.fromisoformat(str(v)[:10])
            except (TypeError, ValueError):
                continue
            days = (d - now.date()).days
            if days >= 0 and (best is None or days < best):
                best = days
            break
    return best


def _oi_history(ticker: str, now: datetime) -> list[list]:
    """The last N settled sessions' OISnapshots (oldest→newest) via `date=`. Phase-2:
    oi-per-strike is `date=` backfillable to ~7 trading days (403 beyond)."""
    sessions: list[list] = []
    d = clock.oi_settled_through(now)
    days: list[date] = []
    for _ in range(_OI_HISTORY_SESSIONS):
        days.append(d)
        d = clock.prev_trading_day(d)
    for sd in reversed(days):                       # oldest → newest
        snaps = _fetch_norm(f"/stock/{ticker}/oi-per-strike", {"date": sd.isoformat()},
                            ticker, Priority.LOW)
        if snaps:
            sessions.append(snaps)
    return sessions


# ── canon assembly + pipeline ─────────────────────────────────────────────────
def build_canon(ticker: str, *, asof: str, now: datetime) -> dict:
    """Fetch every signal's inputs (governor-gated by priority) and assemble the canonical
    map derive_all reads. Each fetch degrades to [] independently."""
    asof_d = date.fromisoformat(asof)
    flow_alerts = _fetch_norm(_FLOW_ALERTS, {"ticker_symbol": ticker, "limit": 500},
                              ticker, Priority.CRITICAL)
    gamma_strikes = _fetch_norm(f"/stock/{ticker}/spot-exposures/strike", {"limit": 500},
                                ticker, Priority.NORMAL)
    iv_term = _fetch_norm(f"/stock/{ticker}/interpolated-iv", {}, ticker, Priority.NORMAL)

    canon: dict = {
        "flow_alerts": flow_alerts,
        "greek_flow": _fetch_norm(f"/stock/{ticker}/greek-flow", {}, ticker, Priority.NORMAL),
        "gamma_strikes": gamma_strikes,
        "skew_rr": _fetch_norm(f"/stock/{ticker}/historical-risk-reversal-skew",
                               {"delta": 25}, ticker, Priority.NORMAL),
        "iv_term": iv_term,
    }

    side = _premium_side(flow_alerts)
    if side:
        canon["flow_side"] = side
        canon["flow_strikes"] = _flow_cluster(flow_alerts, side, asof_d)
        canon["oi_sessions"] = _oi_history(ticker, now)

    # The only MARKET-WIDE input the per-ticker verdict needs: is a high-impact macro event
    # inside the hold window? (routes through Cost). No SPY-gamma / market-tide fetch per
    # ticker — regime as a full read is a future market HEADER, computed once, not here.
    events = _fetch_raw("/market/economic-calendar", None, Priority.LOW)
    canon["event_within_hold"] = _next_high_impact_event(events, now) is not None
    canon["days_to_earnings"] = _days_to_earnings(
        _fetch_raw(f"/stock/{ticker}/earnings", ticker, Priority.LOW), now)
    return canon


def assemble_from_canon(ticker: str, canon: dict, *, asof: str | None = None) -> ViewModel:
    """derive → decide → present over an assembled canon. Pure (REPLAY-reproducible)."""
    signals = derive_all(canon, asof=asof)
    verdict = decide(signals)
    return present(ticker, signals, verdict, as_of=asof)


def assemble(ticker: str, flow_raw: RawRecord, *, asof: str | None = None) -> ViewModel:
    """Flow-only convenience path (normalize one flow-alerts RawRecord → pipeline). Kept
    for the walking-skeleton tests; build_view uses the full canon."""
    return assemble_from_canon(ticker, {"flow_alerts": normalize(flow_raw)}, asof=asof)


def build_view(ticker: str, *, asof: str | None = None) -> ViewModel:
    """Full pipeline for one ticker: build the canon (live, governor-gated) then run the
    pure stages. On total fetch failure, an empty canon still yields a well-formed
    `unavailable`/Stand-down ViewModel (never a crash, never a guessed direction)."""
    ticker = ticker.upper()
    now = datetime.now(tz=timezone.utc)
    asof = asof or clock.session_date(now).isoformat()
    try:
        canon = build_canon(ticker, asof=asof, now=now)
    except Exception:                               # last-resort honest-degrade
        canon = {"flow_alerts": []}
    return assemble_from_canon(ticker, canon, asof=asof)
