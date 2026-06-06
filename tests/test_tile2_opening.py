"""Tile 2 'Opening activity' must measure net-new positioning via
volume_oi_ratio>1 (same proxy as direction + avg_volume_oi_ratio), NOT UW's
all_opening_trades flag, which is ~always False on Basic so opening_pct was
stuck at 0."""
from server.snapshot import _build_tile2
from server.schema import FlowAlert


def _fa(voi):
    return FlowAlert(created_at="2026-06-05T14:00:00Z", strike=100.0, type="call",
                     total_premium=1000.0, total_ask_side_prem=0.0, total_bid_side_prem=0.0,
                     volume_oi_ratio=voi, all_opening_trades=False)


def test_tile2_opening_pct_from_volume_oi_ratio():
    # 3 of 4 alerts are net-new (voi>1) → 75% opening, despite all_opening_trades=False.
    alerts = [_fa(2.0), _fa(1.5), _fa(3.0), _fa(0.5)]
    t2 = _build_tile2(alerts, [], None, 100.0)
    assert round(t2.opening_pct) == 75


def test_tile2_opening_pct_zero_when_no_net_new():
    alerts = [_fa(0.3), _fa(0.9)]   # all closing-ish (voi<=1)
    t2 = _build_tile2(alerts, [], None, 100.0)
    assert t2.opening_pct == 0.0


def test_tile2_strikes_per_side_with_top_expiry_and_premium():
    """Tile 2 strikes are per (strike, side), carry the top expiry by $, and the
    list is highest-premium first (so the frontend defaults to the top contract)."""
    def fa(type_, strike, prem, expiry):
        return FlowAlert(created_at="2026-06-05T14:00:00Z", strike=strike, type=type_,
                         total_premium=prem, total_ask_side_prem=prem, total_bid_side_prem=0,
                         has_singleleg=True, has_multileg=False, underlying_price=100.0,
                         expiry=expiry, volume_oi_ratio=2.0)
    alerts = [
        fa("call", 105.0, 2_000_000, "2026-06-12"),
        fa("call", 105.0, 500_000, "2026-06-19"),   # same (strike,side), smaller, other expiry
        fa("put", 95.0, 900_000, "2026-06-12"),
    ]
    t2 = _build_tile2(alerts, [], None, 100.0)
    by = {(s.strike, s.side): s for s in t2.strikes}
    assert (105.0, "call") in by and (95.0, "put") in by
    c105 = by[(105.0, "call")]
    assert c105.side == "call"
    assert c105.premium_usd == 2_500_000          # both call-105 alerts summed
    assert c105.expiry == "2026-06-12"            # top expiry by $ (2.0M > 0.5M)
    assert t2.strikes[0].premium_usd == 2_500_000  # highest-$ first
