"""Tile 2 positioning-reality: cluster confirmation anchored to the flow side,
never the name-wide call+put aggregate. (The count-based opening_pct was dropped —
opening intensity lives as per-strike vol/OI + Tile 1's $-weighted opening-$ headline.)"""
from datetime import datetime
from zoneinfo import ZoneInfo

from server.snapshot import _build_tile2
from server.schema import FlowAlert


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
    t2 = _build_tile2(alerts, oi_history, direction="calls")
    assert t2.confirmation == "unwinding"        # follows the call side, not the aggregate
    assert t2.oi_trend_5d_pct < 0
    assert t2.flow_side == "call"                 # observed side recorded for the frontend anchor


def test_tile2_context_oi_for_no_flow_side():
    """Settled OI is DECOUPLED from opening flow: a side with no opening flow still
    shows its settled OI at the FLOW side's strikes, as CONTEXT (not a confirmation).
    Puts-only flow → the call side shows call OI at the put-flow strike, flagged
    context; flow_side/confirmation (the verdict input) stay the put side's."""
    fa = FlowAlert(created_at="2026-06-03T14:00:00Z", strike=100.0, type="put",
                   total_premium=1_000_000, total_ask_side_prem=1_000_000, total_bid_side_prem=0,
                   has_singleleg=True, has_multileg=False, expiry="2026-06-12", volume_oi_ratio=2.0)
    oi = [{"date": "2026-06-02", "strikes": {100.0: 0}, "call": {100.0: 500}, "put": {100.0: 1000}},
          {"date": "2026-06-03", "strikes": {100.0: 0}, "call": {100.0: 800}, "put": {100.0: 1200}}]
    t2 = _build_tile2([fa], oi, direction="puts",
                      now_et=datetime(2026, 6, 6, 12, 0, tzinfo=_ET))   # Saturday → all settled
    # Put side: real confirmation (flow there); drives the verdict.
    assert t2.flow_side == "put" and t2.put_is_context is False
    assert t2.confirmation == t2.put_confirmation == "building"
    # Call side: NO flow → CONTEXT (call OI at the put-flow strike), not a confirmation.
    assert t2.call_is_context is True
    assert t2.call_confirmation == "unconfirmed"        # no bet → never "confirms"
    assert [b.oi for b in t2.call_sessions] == [500, 800]
    ctx = [s for s in t2.strikes if s.side == "call" and s.is_context]
    assert ctx and ctx[0].strike == 100.0 and [b.oi for b in ctx[0].sessions] == [500, 800]


def test_tile2_flow_side_empty_when_no_flow():
    """No flow alerts → direction was a gamma guess, so there's no observed flow
    side; flow_side must be "" so the frontend doesn't imply a confirmed side."""
    t2 = _build_tile2([], [], direction="calls")
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
    t2 = _build_tile2(alerts, oi_history, direction="calls")
    assert t2.call_confirmation == "building" and t2.call_oi_trend_pct == 20.0
    assert t2.put_confirmation == "unwinding" and t2.put_oi_trend_pct == -20.0
    # flow_side is calls → top-level confirmation mirrors the call cluster
    assert t2.confirmation == "building"
    # aggregate session series = summed cluster OI per settled session (the primary
    # "did it build" visual the verdict is derived from)
    assert [b.oi for b in t2.call_sessions] == [1000, 1200]
    assert [b.oi for b in t2.put_sessions] == [1000, 800]


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
    t2 = _build_tile2(alerts, [], "calls")
    by = {(s.strike, s.side): s for s in t2.strikes}
    assert (105.0, "call") in by and (95.0, "put") in by
    c105 = by[(105.0, "call")]
    assert c105.side == "call"
    assert c105.premium_usd == 2_500_000          # both call-105 alerts summed
    assert c105.expiry == "2026-06-12"            # top expiry by $ (2.0M > 0.5M)
    assert t2.strikes[0].premium_usd == 2_500_000  # highest-$ first


def _fa_oi(type_, strike, expiry="2026-06-12"):
    return FlowAlert(created_at="2026-06-05T14:00:00Z", strike=strike, type=type_,
                     total_premium=1_000_000, total_ask_side_prem=0, total_bid_side_prem=0,
                     has_singleleg=True, has_multileg=False, expiry=expiry, volume_oi_ratio=2.0)


_ET = ZoneInfo("America/New_York")


def _fa_on(date_iso, type_="call", strike=105.0):
    """A flow alert created at 14:00 UTC on a given ET date (so its ET session date
    is unambiguous)."""
    return FlowAlert(created_at=f"{date_iso}T14:00:00Z", strike=strike, type=type_,
                     total_premium=1_000_000, total_ask_side_prem=0, total_bid_side_prem=0,
                     has_singleleg=True, has_multileg=False, expiry="2026-06-12", volume_oi_ratio=2.0)


def test_tile2_settlement_forming_only_for_todays_live_session():
    """The forming session is ONLY today's live session (a trading day, after the
    9:15 ET open). Evaluated Wed 2026-06-03 noon → today is forming, settling the
    next trading day (Thu). Today's own (provisional) partition is NOT a settled bar."""
    oi = [{"date": "2026-06-01", "strikes": {105.0: 1000}, "call": {105.0: 1000}, "put": {}},
          {"date": "2026-06-02", "strikes": {105.0: 1050}, "call": {105.0: 1050}, "put": {}},
          {"date": "2026-06-03", "strikes": {105.0: 1100}, "call": {105.0: 1100}, "put": {}}]  # today, provisional
    t2 = _build_tile2([_fa_on("2026-06-03")], oi, direction="calls",
                      now_et=datetime(2026, 6, 3, 12, 0, tzinfo=_ET))   # Wed noon
    assert t2.settlement_mode == "forming"
    assert t2.forming_date == "2026-06-03"
    assert t2.settles_on == "2026-06-04"        # Thursday
    assert t2.settled_through == "2026-06-02"    # today's bar excluded


def test_tile2_settlement_weekend_shows_all_days_no_forming():
    """Over the weekend NOTHING is forming (market closed) and every weekday bar
    shows — Friday's session has settled. A phantom Sunday partition (carried-forward
    capture) is dropped, never a distinct session."""
    oi = [{"date": "2026-06-01", "strikes": {105.0: 1000}, "call": {105.0: 1000}, "put": {}},
          {"date": "2026-06-02", "strikes": {105.0: 1010}, "call": {105.0: 1010}, "put": {}},
          {"date": "2026-06-03", "strikes": {105.0: 1020}, "call": {105.0: 1020}, "put": {}},
          {"date": "2026-06-04", "strikes": {105.0: 1030}, "call": {105.0: 1030}, "put": {}},
          {"date": "2026-06-05", "strikes": {105.0: 1040}, "call": {105.0: 1040}, "put": {}},
          {"date": "2026-06-07", "strikes": {105.0: 1040}, "call": {105.0: 1040}, "put": {}}]  # Sun phantom
    t2 = _build_tile2([_fa_on("2026-06-05")], oi, direction="calls",
                      now_et=datetime(2026, 6, 7, 12, 0, tzinfo=_ET))   # Sunday noon
    assert t2.settlement_mode == "settled"
    assert t2.forming_date == ""
    assert t2.settles_on == ""
    assert t2.settled_through == "2026-06-05"     # Friday, the latest weekday
    assert t2.sessions_available == 5             # all five weekdays, Sunday phantom dropped


def test_tile2_settlement_caps_to_five_sessions():
    """The read window is widened to survive weekend/holiday gaps, so when more than
    five settled trading sessions are present the display caps to the most recent 5."""
    oi = [{"date": d, "strikes": {105.0: 1000}, "call": {105.0: 1000}, "put": {}}
          for d in ["2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29",
                    "2026-06-01", "2026-06-02", "2026-06-03"]]   # 7 trading days
    t2 = _build_tile2([_fa_on("2026-06-03")], oi, direction="calls",
                      now_et=datetime(2026, 6, 6, 12, 0, tzinfo=_ET))   # Saturday → nothing forming
    assert t2.sessions_available == 5
    assert t2.settled_through == "2026-06-03"
    # oldest two dropped, newest five kept
    assert [b.date for b in t2.call_sessions] == [
        "2026-05-28", "2026-05-29", "2026-06-01", "2026-06-02", "2026-06-03"]


def test_tile2_settlement_premarket_trading_day_not_yet_forming():
    """Before the 9:15 ET open on a trading day, today's session hasn't begun, so
    nothing is forming yet — the prior settled sessions stand."""
    oi = [{"date": "2026-06-04", "strikes": {105.0: 1000}, "call": {105.0: 1000}, "put": {}},
          {"date": "2026-06-05", "strikes": {105.0: 1100}, "call": {105.0: 1100}, "put": {}}]
    t2 = _build_tile2([_fa_on("2026-06-05")], oi, direction="calls",
                      now_et=datetime(2026, 6, 8, 7, 0, tzinfo=_ET))    # Mon 7am, pre-open
    assert t2.settlement_mode == "settled"
    assert t2.forming_date == ""
    assert t2.settled_through == "2026-06-05"
