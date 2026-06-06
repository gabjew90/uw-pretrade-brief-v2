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
