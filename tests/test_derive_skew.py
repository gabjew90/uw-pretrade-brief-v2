"""derive_skew tests (Phase 4) — golden (real RR-skew bronze → normalize → derive) +
the sign-correction invariant (vendor put−call NEGATED to call−put). The agree/oppose
combination vs direction is decide's job and is tested there.
"""
import json
from pathlib import Path

from server.models import Provenance, Quality, SkewPoint
from server.pipeline.derive import _SKEW_THR, derive_skew
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import normalize

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "historical-risk-reversal-skew" / "SPY.json"


def _pt(date, rr):
    return SkewPoint(date=date, delta=25, risk_reversal=rr)


# ── sign-correction invariant ─────────────────────────────────────────────────
def test_vendor_put_skew_becomes_negative_call_minus_put():
    """Vendor positive (put-skew) → corrected NEGATIVE (call−put) → lean put_skew."""
    s = derive_skew({"skew_rr": [_pt("2026-06-08", 0.05)]})    # vendor +0.05 = put-skew
    assert s.rr25 == -0.05
    assert s.lean == "put_skew"


def test_vendor_negative_becomes_call_skew():
    s = derive_skew({"skew_rr": [_pt("2026-06-08", -0.05)]})   # vendor -0.05 = call-skew
    assert s.rr25 == 0.05
    assert s.lean == "call_skew"


def test_small_magnitude_is_neutral():
    s = derive_skew({"skew_rr": [_pt("2026-06-08", _SKEW_THR / 2)]})
    assert s.lean == "neutral"
    assert s.rr25 is not None                  # value still reported, just no lean


def test_latest_date_wins():
    pts = [_pt("2026-06-01", -0.05), _pt("2026-06-08", 0.05), _pt("2026-06-04", 0.0)]
    s = derive_skew({"skew_rr": pts})
    assert s.lean == "put_skew"                # uses 2026-06-08 (vendor +0.05)


def test_empty_is_unavailable():
    s = derive_skew({"skew_rr": []})
    assert s.lean == "unavailable"
    assert s.provenance.quality == Quality.UNAVAILABLE


def test_missing_key_is_unavailable():
    assert derive_skew({}).lean == "unavailable"


def test_provenance_carried():
    p = Provenance(quality=Quality.DEGRADED, as_of="2026-06-08T00:00:00Z")
    pt = _pt("2026-06-08", 0.05); pt.provenance = p
    s = derive_skew({"skew_rr": [pt]})
    assert s.provenance.quality == Quality.DEGRADED


# ── golden: real RR-skew bronze → normalize → derive ──────────────────────────
def test_golden_real_rr_skew_spy_is_put_skewed():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = RawRecord(endpoint="/stock/SPY/historical-risk-reversal-skew", params={},
                    ticker="SPY", fetched_at="2026-06-08T15:05:00Z", content_hash="h",
                    payload=payload)
    pts = normalize(raw)
    assert len(pts) == 8
    s = derive_skew({"skew_rr": pts})
    # SPY structurally put-skewed: vendor RR positive → corrected negative → put_skew
    assert s.rr25 is not None and s.rr25 < 0
    assert s.lean == "put_skew"
