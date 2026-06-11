"""Pipeline orchestrator — runs the five stages in order for one ticker.

The thin coordinator the HTTP layer calls: ingest → normalize → derive → decide → present.
It owns NO signal math (each stage does its own job); it sequences the typed boundaries,
assembles the canonical map every signal reads, and handles honest-degrade when a fetch or
parse fails (a missing endpoint → that signal `unavailable`, never a crash, never a guess).

Fetch policy (operator: full fetch, governor-gated): direction-critical flow is CRITICAL;
confirming context is NORMAL; market-wide regime context is LOW — so under budget pressure
the governor sheds the nice-to-haves first and the direction read still lands.

The pure helpers (`_flow_cluster`, `_tide_lean`, `_latest_iv`, `_days_to_earnings`) carry
the cross-signal plumbing and are unit-tested offline; the side is picked by the shared
`derive.flow_side` (so verdict/cost/OI never split). The live multi-fetch (`build_canon`,
`_market_now`) needs the network. `assemble_from_canon` is pure given a canon (REPLAY-
reproducible).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from server.models import ViewModel
from server.pipeline.decide import decide
from server.pipeline.derive import (_pick_contract, derive_all, derive_dealer_gamma,
                                     flow_side, next_macro_event, session_alerts)
from server.pipeline.ingest import RawRecord, ingest
from server.pipeline.normalize import NormalizeError, normalize
from server.pipeline.present import present
from server.services import clock, provenance as prov
from server.services.governor import Priority
from server.services.uw_client import UWError

log = logging.getLogger(__name__)
_FLOW_ALERTS = "/option-trades/flow-alerts"
_NEAR_DTE = 14            # flow cluster = near-dated strikes (tile2-confirmation-principle)
_CLUSTER_TOP_N = 5


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


def _fetch_raw(endpoint: str, ticker: str | None, priority: Priority) -> list | None:
    """Ingest one endpoint and return its raw rows (for inputs with no canonical model
    yet — market-tide, economic-calendar, stock-state, earnings). Returns None on FAILURE
    (distinct from [] = a genuinely empty 200) so callers can render "n/a" instead of
    stating a confident wrong all-clear like "no event in 5d" (review SEVERE #2)."""
    try:
        return _rows(ingest(endpoint, {}, ticker=ticker, priority=priority).payload)
    except UWError:
        return None


def _spot_of(ticker: str, priority: Priority) -> float | None:
    """Current spot from stock-state (close, falling back to prev_close)."""
    rows = _fetch_raw(f"/stock/{ticker}/stock-state", ticker, priority)
    for r in rows or []:
        for k in ("close", "last_price", "price", "prev_close"):
            v = _f(r.get(k))
            if v:
                return v
    return None


# Strike band for spot-exposures: without min/max_strike UW returns a ONE-SIDED default
# band (captured SPY: 525→750 with spot 744.94 = -29.5%/+0.68%), which inverts gex_sign and
# fakes walls (review SEVERE #1; the v2 "band-edge garbage" bug). Fetch spot first, bracket it.
_GAMMA_BAND_PCT = 15.0


def _fetch_gamma(ticker: str, priority: Priority) -> tuple[float | None, list]:
    """(spot, gamma_strikes) with the strike band bracketed around spot. Without a spot the
    unbracketed fetch is still made — derive's band guard will then declare it unavailable
    rather than trust a one-sided read."""
    spot = _spot_of(ticker, priority)
    params: dict = {"limit": 500}
    if spot:
        params["min_strike"] = int(spot * (1 - _GAMMA_BAND_PCT / 100))
        params["max_strike"] = int(spot * (1 + _GAMMA_BAND_PCT / 100)) + 1
    return spot, _fetch_norm(f"/stock/{ticker}/spot-exposures/strike", params, ticker, priority)


_FLOW_MAX_PAGES = 3   # extra older_than pages chasing the session start (v2 probed working)


def _fetch_session_flow(ticker: str) -> list:
    """Flow alerts with the NEWEST SESSION complete: the 500-cap page is the most-recent
    tail, so on a busy day the morning is missing — page BACKWARD via older_than (a
    created_at cursor, v2-probed) until the oldest alert leaves the newest session or the
    page budget runs out. Dedupes on (created_at, type, strike, premium). The session
    filter downstream still cuts any prior-session spill."""
    from server.pipeline.derive import _alert_session
    alerts = _fetch_norm(_FLOW_ALERTS, {"ticker_symbol": ticker, "limit": 500},
                         ticker, Priority.CRITICAL)
    pages = 0
    while alerts and pages < _FLOW_MAX_PAGES:
        stamps = sorted(a.created_at for a in alerts if a.created_at)
        if not stamps:
            break
        oldest = stamps[0]
        newest_session = max(_alert_session(a) for a in alerts)
        if _alert_session_of_ts(oldest) != newest_session:
            break                                   # already reach past the session start
        more = _fetch_norm(_FLOW_ALERTS, {"ticker_symbol": ticker, "limit": 500,
                                          "older_than": oldest}, ticker, Priority.NORMAL)
        pages += 1
        if not more:
            break
        seen = {(a.created_at, a.type, a.strike, a.total_premium) for a in alerts}
        fresh = [a for a in more
                 if (a.created_at, a.type, a.strike, a.total_premium) not in seen]
        if not fresh:
            break
        alerts = alerts + fresh
    return alerts


def _alert_session_of_ts(ts: str) -> str:
    from server.pipeline.derive import _alert_session

    class _A:                                       # tiny shim: _alert_session reads .created_at
        created_at = ts
    return _alert_session(_A)


# ── pure cross-signal helpers (unit-tested offline) ───────────────────────────
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


def _skew_expiry(asof_d: date, min_dte: int = 25) -> str:
    """The ~30d MONTHLY expiry the 25Δ RR series is read at: the nearest 3rd-Friday at
    least `min_dte` days out. The no-expiry series is a DIFFERENT (near-tenor) series —
    probed 2026-06-09: latest +0.006 vs +0.049 on the 30d monthly — so the expiry must be
    explicit. Ported from `e1d6c5e:server/snapshot.py::_skew_expiry`."""
    y, m = asof_d.year, asof_d.month
    while True:
        first = date(y, m, 1)
        third_friday = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
        if (third_friday - asof_d).days >= min_dte:
            return third_friday.isoformat()
        m += 1
        if m > 12:
            y, m = y + 1, 1


def session_candles(bars) -> list[dict]:
    """Regular-hours 15m candles for the NEWEST session present in the data (matches the
    flow session by construction) — the price axis the design overlays walls/flip on.
    Pure; chart rows {t,o,h,l,c,v} with t in ET HH:MM."""
    from server.pipeline.derive import _ET
    reg = [b for b in (bars or []) if b.market_time == "r"]
    if not reg:
        return []
    def et(b):
        t = datetime.fromisoformat(b.start_time.replace("Z", "+00:00")).astimezone(_ET)
        return t
    newest = max(et(b).date() for b in reg)
    return [{"t": et(b).strftime("%H:%M"), "o": b.open, "h": b.high, "l": b.low,
             "c": b.close, "v": b.volume}
            for b in reg if et(b).date() == newest]


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
    """Market-tide lean: bull / bear / neutral. market-tide is CUMULATIVE (the fixture runs
    −22M at 09:30 → +162M at 11:00), so the LAST row is the session net — summing the series
    would double-count and can report the wrong lean after an intraday flip (Fix 4a)."""
    if not tide_rows:
        return "neutral"
    last = tide_rows[-1]
    net = (_f(last.get("net_call_premium")) or 0.0) - (_f(last.get("net_put_premium")) or 0.0)
    return "bull" if net > 0 else "bear" if net < 0 else "neutral"


def _latest_iv(iv_term) -> float | None:
    """A representative IV for the regime vol read (the near-term volatility)."""
    if not iv_term:
        return None
    return min(iv_term, key=lambda p: p.days if p.days is not None else 999).volatility


def _market_now(ticker: str, spot: float | None, gamma_strikes: list, iv_term: list,
                now: datetime) -> dict:
    """Raw market context (market-wide, computed ONCE per page load): SPY index gamma sign,
    SPY IV, tide lean, next macro event. NO posture, no Signal — present formats these into
    the Market-today tile, and the event feeds the Cost gate. SPY-based regardless of the
    viewed ticker; reuses viewed data when the view IS SPY. A FAILED calendar/tide fetch is
    None ("n/a"), never an implied all-clear (review SEVERE #2); `events_known` lets Cost
    flag the missing macro check."""
    if ticker == "SPY" and gamma_strikes:
        spy_spot, spy_gamma = spot, gamma_strikes
    else:
        spy_spot, spy_gamma = _fetch_gamma("SPY", Priority.LOW)
    dg = derive_dealer_gamma({"gamma_strikes": spy_gamma, "spot": spy_spot})
    gamma_sign = dg.gex_sign if dg.flip_status != "unavailable" else None
    spy_iv = iv_term if (ticker == "SPY" and iv_term) else \
        _fetch_norm("/stock/SPY/interpolated-iv", {}, "SPY", Priority.LOW)
    tide = _fetch_raw("/market/market-tide", None, Priority.LOW)
    events = _fetch_raw("/market/economic-calendar", None, Priority.LOW)
    events_known = events is not None
    nxt = next_macro_event(events or [], now)
    event_line = None
    if nxt is not None:
        name, days = nxt
        event_line = f"{name} <1d" if days <= 1 else f"{name} {int(round(days))}d"
    return {"gamma_sign": gamma_sign, "iv": _latest_iv(spy_iv),
            "tide": _tide_lean(tide) if tide is not None else None,
            "event_line": event_line, "event_within_hold": nxt is not None,
            "events_known": events_known, "as_of": now.isoformat()}


_CLUSTER_OI_CONTRACTS = 5   # cluster contracts whose OI history is trended


def _cluster_contracts(chain, side: str, strikes: list[float], asof_d: date) -> list:
    """The EXACT contracts the flow cluster points at: flow side, cluster strike, nearest
    near-dated expiry (0..14 DTE) per strike, from the already-fetched chain."""
    picks = []
    for k in strikes:
        cands = []
        for c in chain or []:
            if c.type != side or c.strike != k or not c.symbol:
                continue
            try:
                dte = (date.fromisoformat(c.expiry) - asof_d).days
            except (TypeError, ValueError):
                continue
            if 0 <= dte <= _NEAR_DTE:
                cands.append((dte, c))
        if cands:
            picks.append(min(cands, key=lambda t: t[0])[1])
    return picks[:_CLUSTER_OI_CONTRACTS]


def _cluster_contract_oi(chain, side: str, strikes: list[float], asof_d: date) -> list[list]:
    """Per-contract daily OI history for the cluster contracts (option-contract/{id}/
    historic — the deep OI source; replaces the old 4×date= oi-per-strike loop)."""
    out: list[list] = []
    for c in _cluster_contracts(chain, side, strikes, asof_d):
        bars = _fetch_norm(f"/option-contract/{c.symbol}/historic", {}, c.symbol, Priority.LOW)
        if bars:
            out.append(bars)
    return out


# ── canon assembly + pipeline ─────────────────────────────────────────────────
def build_canon(ticker: str, *, asof: str, now: datetime) -> dict:
    """Fetch every signal's inputs (governor-gated by priority) and assemble the canonical
    map derive_all reads. Each fetch degrades to [] independently."""
    asof_d = date.fromisoformat(asof)
    flow_alerts = _fetch_session_flow(ticker)
    spot, gamma_strikes = _fetch_gamma(ticker, Priority.NORMAL)
    iv_term = _fetch_norm(f"/stock/{ticker}/interpolated-iv", {}, ticker, Priority.NORMAL)

    canon: dict = {
        "flow_alerts": flow_alerts,
        "greek_flow": _fetch_norm(f"/stock/{ticker}/greek-flow", {}, ticker, Priority.NORMAL),
        "gamma_strikes": gamma_strikes,
        "skew_rr": _fetch_norm(f"/stock/{ticker}/historical-risk-reversal-skew",
                               {"expiry": _skew_expiry(asof_d), "delta": 25},
                               ticker, Priority.NORMAL),
        "iv_term": iv_term,
        # the chain for the spread-cost / expected-move gate (load-bearing risk check)
        "option_contracts": _fetch_norm(f"/stock/{ticker}/option-contracts", {"limit": 500},
                                        ticker, Priority.NORMAL),
        "spot": spot or next((g.price for g in gamma_strikes if g.price), None),
        # term structure for the overpay check (front vs back IV)
        "term_structure": _fetch_norm(f"/stock/{ticker}/volatility/term-structure", {},
                                      ticker, Priority.LOW),
        # 15m candles (price axis for the chart UI; regular hours, newest session)
        "ohlc": _fetch_norm(f"/stock/{ticker}/ohlc/15m", {}, ticker, Priority.LOW),
    }

    sess_alerts = session_alerts(flow_alerts)       # newest session only (no prior-day mix)
    side, _basis = flow_side(sess_alerts)           # SAME picker the verdict direction uses
    if side:
        canon["flow_side"] = side
        canon["flow_strikes"] = _flow_cluster(sess_alerts, side, asof_d)
        canon["contract_oi"] = _cluster_contract_oi(canon["option_contracts"], side,
                                                    canon["flow_strikes"], asof_d)
        # greeks for the pick's expiry → delta band + theta drag on the contract guidance
        pick = _pick_contract(canon["option_contracts"], side, canon["spot"] or 0.0, asof_d)
        if pick:
            canon["greeks"] = _fetch_norm(f"/stock/{ticker}/greeks",
                                          {"expiry": pick.expiry}, ticker, Priority.NORMAL)

    earnings = _fetch_raw(f"/stock/{ticker}/earnings", ticker, Priority.LOW)
    canon["days_to_earnings"] = _days_to_earnings(earnings, now)
    canon["earnings_calendar_ok"] = earnings is not None
    return canon


def assemble_from_canon(ticker: str, canon: dict, *, asof: str | None = None,
                        market: dict | None = None) -> ViewModel:
    """derive → decide → present over an assembled canon. Pure (REPLAY-reproducible)."""
    signals = derive_all(canon, asof=asof)
    verdict = decide(signals)
    return present(ticker, signals, verdict, as_of=asof, market=market,
                   candles=session_candles(canon.get("ohlc")))


def assemble(ticker: str, flow_raw: RawRecord, *, asof: str | None = None) -> ViewModel:
    """Flow-only convenience path (normalize one flow-alerts RawRecord → pipeline). Kept
    for the walking-skeleton tests; build_view uses the full canon + regime."""
    return assemble_from_canon(ticker, {"flow_alerts": normalize(flow_raw)}, asof=asof)


_GRID_TOP_N = 12


def grid_from_alerts(alerts) -> list[dict]:
    """Pure: aggregate a cross-ticker flow-alerts pull (newest session only) into the hot
    grid — per ticker the opening-premium totals and the side they lean. Display strings
    are server-built (the frontend computes nothing)."""
    by: dict[str, dict] = {}
    for a in session_alerts(alerts):
        d = by.setdefault(a.ticker, {"call": 0.0, "put": 0.0, "alerts": 0})
        d["alerts"] += 1
        try:
            opening = float(a.volume_oi_ratio or 0.0) > 1.0
        except (TypeError, ValueError):
            opening = False
        if opening:
            d[a.type] += float(a.total_premium or 0.0)

    def money(v):
        return (f"${v/1e9:.1f}B" if v >= 1e9 else f"${v/1e6:.1f}M" if v >= 1e6
                else f"${v/1e3:.0f}K" if v >= 1e3 else f"${v:.0f}")
    rows = []
    for t, d in by.items():
        total = d["call"] + d["put"]
        if total <= 0:
            continue
        # NO side word here: the cross-ticker pull is a SAMPLE (each ticker's most recent
        # alerts only), and a sample-derived side can contradict the brief's full-session
        # direction one tap later (live-caught: grid said SPY PUTS, brief said CALLS).
        # The scanner answers WHO is hot; the brief answers WHICH WAY.
        rows.append({"ticker": t, "premium": total,
                     "premium_fmt": money(total), "call_fmt": money(d["call"]),
                     "put_fmt": money(d["put"]), "alerts": d["alerts"]})
    rows.sort(key=lambda r: r["premium"], reverse=True)
    return rows[:_GRID_TOP_N]


def build_grid() -> dict:
    """The hot-ticker landing grid: ONE cross-ticker flow-alerts call (newest session,
    opening premium by side). Click-through loads the full per-ticker pipeline."""
    try:
        raw = ingest(_FLOW_ALERTS, {"limit": 500}, ticker=None, priority=Priority.NORMAL)
        alerts = normalize(raw)
        rows = grid_from_alerts(alerts)
        return {"rows": rows, "as_of": raw.fetched_at,
                "note": "who's hot: recent-tape sample (latest 500 alerts market-wide). "
                        "open a ticker for its full-session direction"}
    except (UWError, NormalizeError) as e:
        return {"rows": [], "as_of": None, "note": f"grid unavailable: {e}"}


def build_view(ticker: str, *, asof: str | None = None,
               now: datetime | None = None) -> ViewModel:
    """Full pipeline for one ticker: build the canon (live, governor-gated), compute the
    market context once (its macro event feeds the per-ticker Cost gate), then run the pure
    stages. On total fetch failure, an empty canon still yields a well-formed
    `unavailable`/Stand-down ViewModel (never a crash, never a guessed direction).
    `now` is injectable so REPLAY parity is deterministic (clock-service discipline)."""
    ticker = ticker.upper()
    now = now or datetime.now(tz=timezone.utc)
    asof = asof or clock.session_date(now).isoformat()
    try:
        canon = build_canon(ticker, asof=asof, now=now)
        market = _market_now(ticker, canon.get("spot"), canon.get("gamma_strikes") or [],
                             canon.get("iv_term") or [], now)
        canon["event_within_hold"] = market["event_within_hold"]   # macro veto → Cost gate
        canon["event_calendar_ok"] = market["events_known"]        # missing calendar ≠ all-clear
    except Exception:                               # last-resort honest-degrade (Fix 4b)
        log.exception("build_view pipeline error for %s", ticker)
        canon = {"flow_alerts": [], "flow_error": "pipeline error"}   # honest note, not "no flow"
        market = None
    return assemble_from_canon(ticker, canon, asof=asof, market=market)
