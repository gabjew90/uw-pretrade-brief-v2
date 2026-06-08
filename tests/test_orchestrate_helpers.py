"""Unit tests for the orchestrator's pure cross-signal helpers (Phase 4 integration B).
The live multi-fetch is verified end-to-end via the bridge/deploy; these pin the plumbing
that turns canonical records into the cross-signal canon inputs.
"""
from datetime import date, datetime, timezone

from server.models import FlowAlert
from server.pipeline.orchestrate import (_days_to_earnings, _flow_cluster, _latest_iv,
                                         _premium_side, _tide_lean)
from server.models import IVTermPoint

ASOF = date(2026, 6, 8)
NOW = datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc)


def _fa(side, prem, strike, expiry, voi=5.0):
    return FlowAlert(ticker="SPY", type=side, total_premium=prem, volume_oi_ratio=voi,
                     created_at="2026-06-08T15:00:00Z", strike=strike, expiry=expiry)


# ── _premium_side ─────────────────────────────────────────────────────────────
def test_premium_side_picks_dominant():
    alerts = [_fa("call", 100, 600, "2026-06-12"), _fa("put", 500, 590, "2026-06-12")]
    assert _premium_side(alerts) == "put"


def test_premium_side_none_on_empty():
    assert _premium_side([]) is None


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


# ── _tide_lean ────────────────────────────────────────────────────────────────
def test_tide_lean_bull_bear_neutral():
    assert _tide_lean([{"net_call_premium": "100", "net_put_premium": "10"}]) == "bull"
    assert _tide_lean([{"net_call_premium": "10", "net_put_premium": "100"}]) == "bear"
    assert _tide_lean([]) == "neutral"


# ── _latest_iv ────────────────────────────────────────────────────────────────
def test_latest_iv_takes_near_term():
    iv = [IVTermPoint(date="2026-06-08", days=30, volatility=0.25),
          IVTermPoint(date="2026-06-08", days=1, volatility=0.17)]
    assert _latest_iv(iv) == 0.17                 # nearest-term horizon
    assert _latest_iv([]) is None


# ── _days_to_earnings ─────────────────────────────────────────────────────────
def test_days_to_earnings_future_date():
    assert _days_to_earnings([{"report_date": "2026-06-15"}], NOW) == 7


def test_days_to_earnings_none_when_empty_or_unparseable():
    assert _days_to_earnings([], NOW) is None
    assert _days_to_earnings([{"foo": "bar"}], NOW) is None


def test_days_to_earnings_ignores_past():
    assert _days_to_earnings([{"date": "2026-01-01"}], NOW) is None
