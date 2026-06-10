"""Unit tests for the orchestrator's pure cross-signal helpers (Phase 4 integration B).
The live multi-fetch is verified end-to-end via the bridge/deploy; these pin the plumbing
that turns canonical records into the cross-signal canon inputs.
"""
from datetime import date, datetime, timezone

from server.models import FlowAlert
from server.pipeline.orchestrate import (_days_to_earnings, _flow_cluster, _skew_expiry,
                                         _tide_lean)
from server.pipeline.derive import flow_side

ASOF = date(2026, 6, 8)
NOW = datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc)


def _fa(side, prem, strike, expiry, voi=5.0):
    return FlowAlert(ticker="SPY", type=side, total_premium=prem, volume_oi_ratio=voi,
                     created_at="2026-06-08T15:00:00Z", strike=strike, expiry=expiry)


# ── flow_side (the single side-picker: opening leads, total falls back) ───────
def test_flow_side_opening_leads_over_total():
    # opening (voi>1) is put; a bigger CLOSING (voi<=1) call must NOT flip the side
    alerts = [_fa("put", 500, 590, "2026-06-12", voi=5.0),
              _fa("call", 5000, 600, "2026-06-12", voi=0.3)]
    side, basis = flow_side(alerts)
    assert side == "put" and basis == "opening_flow"


def test_flow_side_total_fallback_when_no_opening():
    alerts = [_fa("call", 100, 600, "2026-06-12", voi=0.5),
              _fa("put", 800, 590, "2026-06-12", voi=0.4)]
    side, basis = flow_side(alerts)
    assert side == "put" and basis == "total_flow"


def test_flow_side_none_on_empty():
    assert flow_side([]) == (None, "unavailable")


# ── _tide_lean reads the LAST row of the cumulative series (Fix 4a) ───────────
def test_tide_lean_uses_last_row_after_intraday_flip():
    # cumulative series starts net-negative (bearish) but ends net-positive (bullish):
    # summing would mislabel it; the last tick is the truth.
    series = [{"net_call_premium": "10", "net_put_premium": "100"},   # -90 early
              {"net_call_premium": "200", "net_put_premium": "50"}]   # +150 latest
    assert _tide_lean(series) == "bull"


def test_tide_lean_empty_is_neutral():
    assert _tide_lean([]) == "neutral"


# ── _fetch_session_flow: older_than pagination completes the session (item 5a) ─
def test_session_flow_pages_back_until_session_start(monkeypatch):
    from server.models import FlowAlert
    from server.pipeline import orchestrate as orch

    def fa(ts, prem=1000.0, strike=600.0):
        return FlowAlert(ticker="SPY", type="call", total_premium=prem,
                         volume_oi_ratio=5.0, created_at=ts, strike=strike)

    page1 = [fa(f"2026-06-10T15:{m:02d}:00Z", strike=600 + m) for m in range(10, 30)]
    page2 = ([fa(f"2026-06-10T13:{m:02d}:00Z", strike=500 + m) for m in range(0, 10)]
             + [fa("2026-06-09T19:00:00Z", strike=400)])      # prior session → stop after this

    calls = []

    def fake_norm(endpoint, params, ticker, priority):
        calls.append(params)
        return page2 if "older_than" in params else page1
    monkeypatch.setattr(orch, "_fetch_norm", fake_norm)

    out = orch._fetch_session_flow("SPY")
    assert len(calls) == 2                                    # one follow-up page
    assert calls[1]["older_than"] == "2026-06-10T15:10:00Z"   # cursor = oldest of page 1
    assert len(out) == len(page1) + len(page2)                # morning recovered
    # stops once the oldest alert is from the prior session (covered the session start)


def test_session_flow_no_pagination_when_session_already_covered(monkeypatch):
    from server.models import FlowAlert
    from server.pipeline import orchestrate as orch

    def fa(ts):
        return FlowAlert(ticker="SPY", type="call", total_premium=1000.0,
                         volume_oi_ratio=5.0, created_at=ts)
    page1 = [fa("2026-06-10T15:00:00Z"), fa("2026-06-09T19:00:00Z")]  # already spans sessions
    calls = []

    def fake_norm(endpoint, params, ticker, priority):
        calls.append(params)
        return page1
    monkeypatch.setattr(orch, "_fetch_norm", fake_norm)
    out = orch._fetch_session_flow("SPY")
    assert len(calls) == 1                                    # no follow-up needed
    assert len(out) == 2


# ── grid_from_alerts: the hot-ticker landing scanner (list item 4) ────────────
def test_grid_aggregates_opening_premium_per_ticker():
    from server.pipeline.orchestrate import grid_from_alerts
    from server.models import FlowAlert

    def fa(tkr, side, prem, voi=5.0, ts="2026-06-10T15:00:00Z"):
        return FlowAlert(ticker=tkr, type=side, total_premium=prem, volume_oi_ratio=voi,
                         created_at=ts)
    rows = grid_from_alerts([
        fa("NVDA", "call", 9_000_000), fa("NVDA", "put", 1_000_000),
        fa("TSLA", "put", 5_000_000),
        fa("TSLA", "call", 50_000_000, voi=0.2),   # CLOSING — excluded from premium
        fa("AAPL", "call", 2_000_000, ts="2026-06-09T15:00:00Z"),  # prior session — excluded
    ])
    assert [r["ticker"] for r in rows] == ["NVDA", "TSLA"]   # by opening premium desc
    nvda = rows[0]
    assert nvda["side"] == "CALLS" and nvda["premium_fmt"] == "$10.0M"
    tsla = rows[1]
    assert tsla["side"] == "PUTS" and tsla["premium_fmt"] == "$5.0M"
    assert tsla["alerts"] == 2                               # closing alert still counted


def test_grid_empty_alerts():
    from server.pipeline.orchestrate import grid_from_alerts
    assert grid_from_alerts([]) == []


# ── _skew_expiry: nearest 3rd-Friday monthly >= 25 DTE ────────────────────────
def test_skew_expiry_picks_3rd_friday_at_least_25d_out():
    # 2026-06-09: June 3rd Friday = 2026-06-19 (10d, too near) -> July 17 (38d)
    assert _skew_expiry(date(2026, 6, 9)) == "2026-07-17"


def test_skew_expiry_uses_current_month_when_far_enough():
    # 2026-06-01: June 19 is 18d (too near) -> July 17; 2026-05-20: June 19 = 30d OK
    assert _skew_expiry(date(2026, 5, 20)) == "2026-06-19"


def test_skew_expiry_year_rollover():
    # 2026-12-10: Dec 3rd Friday = 2026-12-18 (8d) -> 2027-01-15
    assert _skew_expiry(date(2026, 12, 10)) == "2027-01-15"


# ── Market vol uses SPY IV, never the viewed ticker's (Fix 3) ─────────────────
def test_market_now_uses_spy_iv_not_viewed_ticker(monkeypatch):
    from datetime import datetime, timezone
    from server.models import GammaStrike, IVTermPoint
    from server.pipeline import orchestrate as orch

    now = datetime(2026, 6, 9, 15, 0, tzinfo=timezone.utc)

    def fake_norm(endpoint, params, ticker, priority):
        if "spot-exposures" in endpoint:
            return [GammaStrike(strike=600, call_gamma_oi=1.0, put_gamma_oi=-2.0, price=600.0)]
        if "interpolated-iv" in endpoint:
            return [IVTermPoint(date="2026-06-09", days=5, volatility=0.30)]   # SPY IV
        return []
    monkeypatch.setattr(orch, "_fetch_norm", fake_norm)
    monkeypatch.setattr(orch, "_fetch_raw", lambda *a, **k: [])

    tsla_iv = [IVTermPoint(date="2026-06-09", days=5, volatility=0.99)]        # viewed ticker
    m = orch._market_now("TSLA", None, [], tsla_iv, now)
    assert m["iv"] == 0.30                          # SPY's IV, NOT TSLA's 0.99


def test_market_now_spy_iv_fetch_fails_degrades_to_none(monkeypatch):
    from datetime import datetime, timezone
    from server.pipeline import orchestrate as orch
    now = datetime(2026, 6, 9, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(orch, "_fetch_norm", lambda *a, **k: [])    # every fetch fails/empty
    monkeypatch.setattr(orch, "_fetch_raw", lambda *a, **k: None)   # raw fetches FAIL
    m = orch._market_now("TSLA", None, [], [], now)
    assert m["iv"] is None                          # never the viewed ticker's number
    assert m["events_known"] is False               # failed calendar ≠ all-clear
    assert m["tide"] is None


# ── _flow_cluster ─────────────────────────────────────────────────────────────
def test_flow_cluster_near_dated_top_strikes():
    alerts = [
        _fa("call", 900, 600, "2026-06-12"),     # near, biggest
        _fa("call", 300, 605, "2026-06-12"),     # near
        _fa("call", 999, 610, "2026-12-18"),     # FAR (>14 DTE) — excluded despite size
        _fa("put", 999, 590, "2026-06-12"),      # wrong side — excluded
    ]
    cluster = _flow_cluster(alerts, "call", ASOF, top_n=5, near=14)
    assert 600 in cluster and 605 in cluster
    assert 610 not in cluster                     # far-dated excluded (tile2 near-dated rule)


def test_flow_cluster_respects_top_n():
    alerts = [_fa("call", 100 * i, 600 + i, "2026-06-12") for i in range(1, 9)]
    assert len(_flow_cluster(alerts, "call", ASOF, top_n=3)) == 3


# ── _days_to_earnings ─────────────────────────────────────────────────────────
def test_days_to_earnings_future_date():
    assert _days_to_earnings([{"report_date": "2026-06-15"}], NOW) == 7


def test_days_to_earnings_none_when_empty_or_unparseable():
    assert _days_to_earnings([], NOW) is None
    assert _days_to_earnings([{"foo": "bar"}], NOW) is None


def test_days_to_earnings_ignores_past():
    assert _days_to_earnings([{"date": "2026-01-01"}], NOW) is None
