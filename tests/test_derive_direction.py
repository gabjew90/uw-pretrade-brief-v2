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


def test_empty_flow_is_unavailable_not_guessed():
    f = derive_direction({"flow_alerts": []})
    assert f.direction is None
    assert f.direction_basis == "unavailable"
    assert f.provenance.quality == Quality.UNAVAILABLE


def test_missing_key_is_unavailable():
    f = derive_direction({})
    assert f.direction is None and f.direction_basis == "unavailable"


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
