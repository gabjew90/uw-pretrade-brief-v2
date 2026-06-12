"""derive_direction tests (Phase 3 walking skeleton) — golden (real bronze → normalize →
derive) + sign-invariant property tests. The pure function is where sign inversions are
caught before they ship.
"""
import json
from pathlib import Path

from server.models import FlowAlert, Provenance, Quality
from server.pipeline.derive import derive_direction
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import normalize

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "flow-alerts" / "SPY.json"


def _fa(side, prem, voi, **over):
    return FlowAlert(ticker="SPY", type=side, total_premium=prem, volume_oi_ratio=voi,
                     created_at="2026-06-08T15:00:00Z", **over)


# ── property / invariant ──────────────────────────────────────────────────────
def test_opening_call_dominates_gives_calls():
    alerts = [_fa("call", 1_000_000, 5.0), _fa("put", 100_000, 5.0)]
    f = derive_direction({"flow_alerts": alerts})
    assert f.direction == "calls"
    assert f.direction_basis == "opening_flow"
    assert f.call_prem == 1_000_000 and f.put_prem == 100_000


def test_opening_put_dominates_gives_puts():
    alerts = [_fa("call", 100_000, 5.0), _fa("put", 900_000, 5.0)]
    f = derive_direction({"flow_alerts": alerts})
    assert f.direction == "puts"
    assert f.direction_basis == "opening_flow"


def test_opening_flow_leads_even_when_total_disagrees():
    """Closing (voi<=1) flow on the other side must NOT override opening flow."""
    alerts = [
        _fa("call", 500_000, 5.0),     # opening calls
        _fa("put", 5_000_000, 0.3),    # huge but CLOSING (voi<=1) puts — must not lead
    ]
    f = derive_direction({"flow_alerts": alerts})
    assert f.direction == "calls"
    assert f.direction_basis == "opening_flow"


def test_no_opening_flow_falls_back_to_total():
    alerts = [_fa("call", 100_000, 0.5), _fa("put", 800_000, 0.4)]   # all closing
    f = derive_direction({"flow_alerts": alerts})
    assert f.direction == "puts"
    assert f.direction_basis == "total_flow"


# ── the receipts: top_alerts lists the biggest bets behind the read ───────────
def test_top_alerts_are_the_biggest_opening_bets_in_et():
    alerts = [
        _fa("call", 2_000_000, 5.0, strike=580.0, expiry="2026-06-13",
            total_ask_side_prem=1_500_000.0, total_bid_side_prem=400_000.0),
        _fa("call", 300_000, 4.0, strike=585.0),
        _fa("put", 9_000_000, 0.4, strike=570.0),   # huge but CLOSING — not a receipt
    ]
    f = derive_direction({"flow_alerts": alerts})
    assert f.direction_basis == "opening_flow"
    assert len(f.top_alerts) == 2                   # closing alert excluded
    top = f.top_alerts[0]
    assert top["premium"] == 2_000_000 and top["strike"] == 580.0
    assert top["aggressor"] == "ask-side"           # buyer was the aggressor
    assert top["time"] == "11:00"                   # 15:00Z -> ET
    assert f.top_alerts[0]["premium"] >= f.top_alerts[1]["premium"]


# ── intraday arrival: flow_series + late_pct (WHEN the bets came) ─────────────
def test_flow_series_is_cumulative_by_side_in_et():
    alerts = [
        _fa("call", 600_000, 5.0),                                        # 15:00Z = 11:00 ET
        FlowAlert(ticker="SPY", type="put", total_premium=400_000, volume_oi_ratio=5.0,
                  created_at="2026-06-08T18:30:00Z"),                      # 14:30 ET — late
        FlowAlert(ticker="SPY", type="call", total_premium=1_000_000, volume_oi_ratio=5.0,
                  created_at="2026-06-08T19:00:00Z"),                      # 15:00 ET — late
    ]
    f = derive_direction({"flow_alerts": alerts})
    assert [p["t"] for p in f.flow_series] == ["11:00", "14:30", "15:00"]
    assert f.flow_series[-1] == {"t": "15:00", "call": 1_600_000, "put": 400_000}
    assert f.flow_series[0]["call"] == 600_000 and f.flow_series[0]["put"] == 0
    assert f.late_pct == 70.0                  # 1.4M of 2M arrived after 14:00 ET


def test_flow_series_excludes_closing_when_opening_leads():
    alerts = [_fa("call", 500_000, 5.0), _fa("put", 9_000_000, 0.3)]   # put is closing
    f = derive_direction({"flow_alerts": alerts})
    assert f.flow_series[-1]["put"] == 0       # closing premium never enters the timeline


# ── the side's own bar (reviewer 2026-06-11): dominance ratio + premium floor ──
def test_dominant_lean_over_floor_is_qualified():
    alerts = [_fa("call", 1_000_000, 5.0), _fa("put", 100_000, 5.0)]   # 10:1, $1M
    f = derive_direction({"flow_alerts": alerts})
    assert f.lean_quality == "qualified"
    assert f.lean_ratio == 10.0
    assert f.lean_note == "10:1"


def test_near_even_lean_is_weak():
    """A 1.3:1 lean is a coin flip even with big dollars — must not ride to Favorable."""
    alerts = [_fa("call", 1_300_000, 5.0), _fa("put", 1_000_000, 5.0)]
    f = derive_direction({"flow_alerts": alerts})
    assert f.direction == "calls"                       # side still picked (shown honestly)
    assert f.lean_quality == "weak"
    assert f.lean_note == "1.3:1"


def test_thin_premium_is_weak_even_if_one_sided():
    """$400K of one-sided premium on a big name is noise — the absolute floor catches it."""
    alerts = [_fa("call", 400_000, 5.0)]
    f = derive_direction({"flow_alerts": alerts})
    assert f.lean_quality == "weak"
    assert "thin $400K" in f.lean_note


def test_one_sided_over_floor_is_qualified():
    f = derive_direction({"flow_alerts": [_fa("put", 900_000, 5.0)]})
    assert f.lean_quality == "qualified"
    assert f.lean_ratio is None                         # no loser premium
    assert f.lean_note == "one-sided"


def test_empty_flow_is_unavailable_not_guessed():
    f = derive_direction({"flow_alerts": []})
    assert f.direction is None
    assert f.direction_basis == "unavailable"
    assert f.provenance.quality == Quality.UNAVAILABLE


def test_missing_key_is_unavailable():
    f = derive_direction({})
    assert f.direction is None and f.direction_basis == "unavailable"


def test_pipeline_error_note_is_honest(_=None):
    """Fix 4b: a build_view crash sets flow_error so the user sees the real cause, not the
    misleading 'no flow alerts'."""
    f = derive_direction({"flow_alerts": [], "flow_error": "pipeline error"})
    assert f.direction is None
    assert f.provenance.note == "pipeline error"


def test_zero_premium_both_sides_unavailable():
    alerts = [_fa("call", 0, 5.0), _fa("put", 0, 5.0)]
    f = derive_direction({"flow_alerts": alerts})
    assert f.direction is None and f.direction_basis == "unavailable"


def test_tie_breaks_to_calls():
    alerts = [_fa("call", 500_000, 5.0), _fa("put", 500_000, 5.0)]
    assert derive_direction({"flow_alerts": alerts}).direction == "calls"


def test_provenance_is_derived_from_alerts():
    p = Provenance(quality=Quality.DEGRADED, as_of="2026-06-08T15:00:00Z")
    alerts = [_fa("call", 1_000_000, 5.0, provenance=p)]
    f = derive_direction({"flow_alerts": alerts})
    assert f.provenance.quality == Quality.DEGRADED      # worst-case carried through
    assert f.provenance.as_of == "2026-06-08T15:00:00Z"


# ── session window: the 500-cap tail must not mix sessions ───────────────────
def test_session_filter_drops_prior_day_flow():
    """A pull spanning Fri+Mon must read ONLY Monday: a huge Friday call bias cannot flip
    Monday's put direction (staleness-review fix)."""
    from server.pipeline.derive import session_alerts
    from server.models import FlowAlert

    def fa(side, prem, ts):
        return FlowAlert(ticker="SPY", type=side, total_premium=prem, volume_oi_ratio=5.0,
                         created_at=ts)
    alerts = [
        fa("call", 50_000_000, "2026-06-05T19:00:00Z"),   # Friday RTH (ET 15:00) — stale
        fa("put", 1_000_000, "2026-06-08T14:00:00Z"),     # Monday
        fa("call", 200_000, "2026-06-08T15:00:00Z"),      # Monday
    ]
    kept = session_alerts(alerts)
    assert len(kept) == 2 and all(a.created_at.startswith("2026-06-08") for a in kept)
    f = derive_direction({"flow_alerts": alerts})
    assert f.direction == "puts"                          # Friday's $50M call ignored
    assert f.call_prem == 200_000 and f.put_prem == 1_000_000


def test_session_filter_et_boundary():
    """A 00:30 UTC Tuesday alert is Monday 20:30 ET — same ET session as Monday RTH."""
    from server.pipeline.derive import session_alerts
    from server.models import FlowAlert

    def fa(ts):
        return FlowAlert(ticker="SPY", type="call", total_premium=1, volume_oi_ratio=5.0,
                         created_at=ts)
    kept = session_alerts([fa("2026-06-08T15:00:00Z"), fa("2026-06-09T00:30:00Z")])
    assert len(kept) == 2                                  # both are ET 2026-06-08


# ── golden: real bronze → normalize → derive ──────────────────────────────────
def test_golden_real_flow_alerts_yields_sane_direction():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = RawRecord(endpoint="/option-trades/flow-alerts", params={}, ticker="SPY",
                    fetched_at="2026-06-08T15:05:00Z", content_hash="h", payload=payload)
    alerts = normalize(raw)
    f = derive_direction({"flow_alerts": alerts})
    assert f.direction in ("calls", "puts")               # a real, non-None side
    assert f.direction_basis in ("opening_flow", "total_flow")
    assert (f.call_prem > 0) or (f.put_prem > 0)


def test_golden_excludes_prior_session_premium():
    """The golden pull spans Fri 16:53 ET → Mon: derived premiums must equal the MONDAY
    subset only, strictly less than the whole-pull sums."""
    from server.pipeline.derive import session_alerts
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = RawRecord(endpoint="/option-trades/flow-alerts", params={}, ticker="SPY",
                    fetched_at="2026-06-08T15:05:00Z", content_hash="h", payload=payload)
    alerts = normalize(raw)
    kept = session_alerts(alerts)
    assert 0 < len(kept) < len(alerts)                    # the fixture IS multi-session
    f = derive_direction({"flow_alerts": alerts})
    whole_call = sum(a.total_premium for a in alerts if a.type == "call" and a.volume_oi_ratio > 1)
    sess_call = sum(a.total_premium for a in kept if a.type == "call" and a.volume_oi_ratio > 1)
    assert f.call_prem == sess_call
    assert f.call_prem < whole_call                       # Friday's flow excluded
