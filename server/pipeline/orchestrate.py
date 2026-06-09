"""Pipeline orchestrator — runs the five stages in order for one ticker.

The thin coordinator the HTTP layer calls: ingest → normalize → derive → decide → present.
It owns NO signal math (each stage does its own job); it sequences the typed boundaries,
assembles the canonical map every signal reads, and handles honest-degrade when a fetch or
parse fails (a missing endpoint → that signal `unavailable`, never a crash, never a guess).

Fetch policy (operator: full fetch, governor-gated): direction-critical flow is CRITICAL;
confirming context is NORMAL; market-wide regime context is LOW — so under budget pressure
the governor sheds the nice-to-haves first and the direction read still lands.

The pure helpers (`_premium_side`, `_flow_cluster`, `_tide_lean`, `_latest_iv`,
`_days_to_earnings`) carry the cross-signal plumbing and are unit-tested offline; the live
multi-fetch (`build_canon`, `_market_regime`) needs the network. `assemble_from_canon` is
pure given a canon + regime (REPLAY-reproducible).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from server.models import Regime, ViewModel
from server.pipeline.decide import decide
from server.pipeline.derive import derive_all, derive_dealer_gamma, derive_regime
from server.pipeline.ingest import RawRecord, ingest
from server.pipeline.normalize import NormalizeError, normalize
from server.pipeline.present import present
from server.services import clock, provenance as prov
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


def _tide_lean(tide_rows) -> str:
    """Market-tide lean from net premium across the session: bull / bear / neutral."""
    net = 0.0
    for r in tide_rows:
        net += (_f(r.get("net_call_premium")) or 0.0) - (_f(r.get("net_put_premium")) or 0.0)
    return "bull" if net > 0 else "bear" if net < 0 else "neutral"


def _latest_iv(iv_term) -> float | None:
    """A representative IV for the regime vol read (the near-term volatility)."""
    if not iv_term:
        return None
    return min(iv_term, key=lambda p: p.days if p.days is not None else 999).volatility


def _market_regime(ticker: str, gamma_strikes: list, iv_term: list, now: datetime) -> Regime:
    """Market-wide regime (SPY-based regardless of the viewed ticker) — computed ONCE per
    page load for the header. Reuses the viewed gamma when the view IS SPY to skip a fetch."""
    spy_gamma = gamma_strikes if (ticker == "SPY" and gamma_strikes) else \
        _fetch_norm("/stock/SPY/spot-exposures/strike", {"limit": 500}, "SPY", Priority.LOW)
    dg = derive_dealer_gamma({"gamma_strikes": spy_gamma})
    gamma = {"sign": dg.gex_sign, "status": "ok" if dg.flip_status != "unavailable" else "unavailable"}
    events = _fetch_raw("/market/economic-calendar", None, Priority.LOW)
    tide = _fetch_raw("/market/market-tide", None, Priority.LOW)
    reg = derive_regime({"regime": {
        "gamma": gamma, "vol": {"iv": _latest_iv(iv_term), "rv": None, "trend": None},
        "events": events, "tide": {"lean": _tide_lean(tide)}, "opex": False, "now": now}})
    reg.provenance = prov.live(now.isoformat())     # computed from live market data fetched now
    return reg


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
        # the chain for the spread-cost / expected-move gate (load-bearing risk check)
        "option_contracts": _fetch_norm(f"/stock/{ticker}/option-contracts", {"limit": 500},
                                        ticker, Priority.NORMAL),
        "spot": next((g.price for g in gamma_strikes if g.price), None),
        # term structure for the overpay check (front vs back IV)
        "term_structure": _fetch_norm(f"/stock/{ticker}/volatility/term-structure", {},
                                      ticker, Priority.LOW),
    }

    side = _premium_side(flow_alerts)
    if side:
        canon["flow_side"] = side
        canon["flow_strikes"] = _flow_cluster(flow_alerts, side, asof_d)
        canon["oi_sessions"] = _oi_history(ticker, now)

    canon["days_to_earnings"] = _days_to_earnings(
        _fetch_raw(f"/stock/{ticker}/earnings", ticker, Priority.LOW), now)
    return canon


def assemble_from_canon(ticker: str, canon: dict, *, asof: str | None = None,
                        regime: Regime | None = None) -> ViewModel:
    """derive → decide → present over an assembled canon. Pure (REPLAY-reproducible)."""
    signals = derive_all(canon, asof=asof)
    verdict = decide(signals)
    return present(ticker, signals, verdict, as_of=asof, regime=regime)


def assemble(ticker: str, flow_raw: RawRecord, *, asof: str | None = None) -> ViewModel:
    """Flow-only convenience path (normalize one flow-alerts RawRecord → pipeline). Kept
    for the walking-skeleton tests; build_view uses the full canon + regime."""
    return assemble_from_canon(ticker, {"flow_alerts": normalize(flow_raw)}, asof=asof)


def build_view(ticker: str, *, asof: str | None = None) -> ViewModel:
    """Full pipeline for one ticker: build the canon (live, governor-gated), compute the
    market regime once (its macro event feeds the per-ticker Cost gate), then run the pure
    stages. On total fetch failure, an empty canon still yields a well-formed
    `unavailable`/Stand-down ViewModel (never a crash, never a guessed direction)."""
    ticker = ticker.upper()
    now = datetime.now(tz=timezone.utc)
    asof = asof or clock.session_date(now).isoformat()
    try:
        canon = build_canon(ticker, asof=asof, now=now)
        regime = _market_regime(ticker, canon.get("gamma_strikes") or [],
                                canon.get("iv_term") or [], now)
        canon["event_within_hold"] = regime.event_within_hold   # macro veto → Cost gate
    except Exception:                               # last-resort honest-degrade
        canon, regime = {"flow_alerts": []}, None
    return assemble_from_canon(ticker, canon, asof=asof, regime=regime)
