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


def test_tile2_opening_pct_is_flow_side_only():
    """opening_pct measures the DIRECTION's flow, not the name-wide aggregate.
    With direction=calls, only the call alerts' opening fraction should count —
    contaminating put flow (even if all opening) must not move it."""
    def fa(type_, voi):
        return FlowAlert(created_at="2026-06-05T14:00:00Z", strike=(105.0 if type_ == "call" else 95.0),
                         type=type_, total_premium=1_000.0, total_ask_side_prem=0.0,
                         total_bid_side_prem=0.0, volume_oi_ratio=voi)
    # call side: 1 of 2 opening (50%); put side: both opening (would be 75% if pooled)
    alerts = [fa("call", 2.0), fa("call", 0.5), fa("put", 3.0), fa("put", 3.0)]
    t2 = _build_tile2(alerts, [], None, 100.0, direction="calls")
    assert round(t2.opening_pct) == 50


def test_tile2_confirmation_anchors_to_flow_side_not_aggregate():
    """The aggregate trap: put OI explodes while the flow-side (call) OI shrinks.
    Aggregate call+put OI would read 'building'; the confirmation must follow the
    flow's side (calls) and read 'unwinding'."""
    def fa(type_, strike, prem):
        return FlowAlert(created_at="2026-06-05T14:00:00Z", strike=strike, type=type_,
                         total_premium=prem, total_ask_side_prem=prem, total_bid_side_prem=0.0,
                         has_singleleg=True, has_multileg=False, expiry="2026-06-12",
                         volume_oi_ratio=2.0)
    # Flow on BOTH sides; calls carry more premium → direction=calls.
    alerts = [fa("call", 105.0, 2_000_000), fa("put", 95.0, 1_000_000)]
    # Two settled sessions: call-105 OI shrinks 1000→800 (unwinding on the flow side);
    # put-95 OI explodes 100→9000 (would dominate any aggregate).
    oi_history = [
        {"date": "2020-01-01", "strikes": {105.0: 1100, 95.0: 100},
         "call": {105.0: 1000}, "put": {95.0: 100}},
        {"date": "2020-01-02", "strikes": {105.0: 9800, 95.0: 9000},
         "call": {105.0: 800}, "put": {95.0: 9000}},
    ]
    t2 = _build_tile2(alerts, oi_history, None, 100.0, direction="calls")
    assert t2.confirmation == "unwinding"        # follows the call side, not the aggregate
    assert t2.oi_trend_5d_pct < 0
    assert t2.flow_side == "call"                 # observed side recorded for the frontend anchor


def test_tile2_flow_side_empty_when_no_flow():
    """No flow alerts → direction was a gamma guess, so there's no observed flow
    side; flow_side must be "" so the frontend doesn't imply a confirmed side."""
    t2 = _build_tile2([], [], None, 100.0, direction="calls")
    assert t2.flow_side == ""


def test_tile2_computes_both_side_clusters():
    """Both call and put clusters are computed + shown (the comparison IS the
    signal), each a 5-session trend over its own flow-hit strikes — independent of
    which side the direction picked."""
    def fa(type_, strike, prem):
        return FlowAlert(created_at="2026-06-05T14:00:00Z", strike=strike, type=type_,
                         total_premium=prem, total_ask_side_prem=prem, total_bid_side_prem=0.0,
                         has_singleleg=True, has_multileg=False, expiry="2026-06-12",
                         volume_oi_ratio=2.0)
    alerts = [fa("call", 105.0, 2_000_000), fa("put", 95.0, 1_000_000)]
    # call-105 builds 1000→1200 (+20%); put-95 unwinds 1000→800 (−20%)
    oi_history = [
        {"date": "2020-01-01", "strikes": {105.0: 2000, 95.0: 1000},
         "call": {105.0: 1000}, "put": {95.0: 1000}},
        {"date": "2020-01-02", "strikes": {105.0: 2000, 95.0: 1000},
         "call": {105.0: 1200}, "put": {95.0: 800}},
    ]
    t2 = _build_tile2(alerts, oi_history, None, 100.0, direction="calls")
    assert t2.call_confirmation == "building" and t2.call_oi_trend_pct == 20.0
    assert t2.put_confirmation == "unwinding" and t2.put_oi_trend_pct == -20.0
    # flow_side is calls → top-level confirmation mirrors the call cluster
    assert t2.confirmation == "building"


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
