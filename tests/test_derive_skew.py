"""derive_skew tests (Phase 4, Fix 1) — skew is CHANGE vs the ticker's own trailing
baseline, not a fixed level (RR rides a structurally negative baseline on most names).
Sign-correction (vendor put−call NEGATED to call−put) is unchanged.
"""
import json
from pathlib import Path

from server.models import Provenance, Quality, SkewPoint
from server.pipeline.derive import _SKEW_DELTA_THR, derive_skew
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import normalize

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "historical-risk-reversal-skew" / "SPY.json"


def _pt(date, rr):
    return SkewPoint(date=date, delta=25, risk_reversal=rr)


def _series(*vendor_rrs):
    """Build dated SkewPoints oldest→newest from vendor RR values."""
    return [_pt(f"2026-06-{1 + i:02d}", v) for i, v in enumerate(vendor_rrs)]


# ── change-vs-baseline ────────────────────────────────────────────────────────
def test_today_more_call_bid_than_normal_is_call_skew():
    # baseline vendor ~0.05 (put-rich normally); today vendor 0.00 (much less put-rich) →
    # call−put: baseline -0.05, today 0.00, delta +0.05 → calls richer than usual
    s = derive_skew({"skew_rr": _series(0.05, 0.05, 0.05, 0.00)})
    assert s.lean == "call_skew"
    assert s.rr_delta > _SKEW_DELTA_THR


def test_today_more_put_bid_than_normal_is_put_skew():
    # baseline vendor ~0.01; today vendor 0.06 (much MORE put-rich) → call−put delta negative
    s = derive_skew({"skew_rr": _series(0.01, 0.01, 0.01, 0.06)})
    assert s.lean == "put_skew"
    assert s.rr_delta < -_SKEW_DELTA_THR


def test_flat_history_is_neutral():
    s = derive_skew({"skew_rr": _series(0.03, 0.03, 0.03, 0.03)})
    assert s.lean == "neutral"
    assert abs(s.rr_delta) < _SKEW_DELTA_THR
    assert s.rr25 is not None and s.rr_baseline is not None     # values still reported


def test_baseline_excludes_today():
    s = derive_skew({"skew_rr": _series(0.02, 0.02, 0.02, 0.10)})
    # baseline = mean of the three 0.02 priors → call−put -0.02; today call−put -0.10
    assert round(s.rr_baseline, 3) == -0.02
    assert round(s.rr25, 3) == -0.10


def test_too_few_priors_is_unavailable():
    s = derive_skew({"skew_rr": _series(0.03, 0.03, 0.03)})       # only 2 priors + today
    assert s.lean == "unavailable"
    assert s.provenance.quality == Quality.UNAVAILABLE
    assert "insufficient" in s.provenance.note


def test_empty_is_unavailable():
    assert derive_skew({"skew_rr": []}).lean == "unavailable"


def test_sign_convention_round_trip():
    """Vendor positive (put-rich) negates to negative call−put for both today and baseline."""
    s = derive_skew({"skew_rr": _series(0.04, 0.04, 0.04, 0.04)})
    assert s.rr25 < 0 and s.rr_baseline < 0                       # SPY structurally put-skewed


def test_provenance_carried():
    pts = _series(0.04, 0.04, 0.04, 0.05)
    p = Provenance(quality=Quality.DEGRADED, as_of="2026-06-08T00:00:00Z")
    pts[-1].provenance = p
    assert derive_skew({"skew_rr": pts}).provenance.quality == Quality.DEGRADED


# ── golden: real RR-skew bronze (8 days) → normalize → derive ─────────────────
def test_golden_real_rr_skew_spy():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = RawRecord(endpoint="/stock/SPY/historical-risk-reversal-skew", params={},
                    ticker="SPY", fetched_at="2026-06-08T15:05:00Z", content_hash="h",
                    payload=payload)
    pts = normalize(raw)
    assert len(pts) == 8
    s = derive_skew({"skew_rr": pts})
    # today (0.044 vendor) is MORE put-rich than the ~0.031 baseline → call−put delta < 0
    assert s.lean == "put_skew"
    assert s.rr25 < 0 and s.rr_baseline < 0 and s.rr_delta < 0
