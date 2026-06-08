"""derive_cost tests (Phase 4) — golden (real interpolated-iv → normalize → derive) +
the guard precedence (earnings/event block beats IV; missing IV → caution, never silent ok).
"""
import json
from pathlib import Path

from server.models import IVTermPoint, Quality
from server.pipeline.derive import derive_cost
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import normalize

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "interpolated-iv" / "SPY.json"


def _iv(days, percentile):
    return IVTermPoint(date="2026-06-08", days=days, percentile=percentile)


def _term(*pairs):
    return [_iv(d, p) for d, p in pairs]


# ── IV-rank bands ─────────────────────────────────────────────────────────────
def test_low_ivr_is_ok():
    c = derive_cost({"iv_term": _term((30, 0.40))})       # ivr 40 <= 60
    assert c.guard == "ok"
    assert c.ivr == 40.0


def test_mid_ivr_is_caution():
    c = derive_cost({"iv_term": _term((30, 0.70))})       # 60 < 70 <= 80
    assert c.guard == "caution"


def test_high_ivr_is_block():
    c = derive_cost({"iv_term": _term((30, 0.95))})       # > 80
    assert c.guard == "block"


def test_ivr_uses_row_nearest_30d():
    c = derive_cost({"iv_term": _term((1, 0.95), (30, 0.30), (60, 0.95))})
    assert c.ivr == 30.0 and c.guard == "ok"              # picked the 30d row


# ── guard precedence: events block regardless of IV ───────────────────────────
def test_earnings_within_window_blocks_even_if_iv_cheap():
    c = derive_cost({"iv_term": _term((30, 0.10)), "days_to_earnings": 3})
    assert c.guard == "block"
    assert "earnings" in c.reason
    assert c.days_to_earnings == 3


def test_earnings_outside_window_does_not_block():
    c = derive_cost({"iv_term": _term((30, 0.10)), "days_to_earnings": 30})
    assert c.guard == "ok"


def test_macro_event_within_hold_blocks():
    c = derive_cost({"iv_term": _term((30, 0.10)), "event_within_hold": True})
    assert c.guard == "block"
    assert "macro" in c.reason
    assert c.event_within_hold is True


def test_ivr_is_computed_even_when_blocked():
    """The supporting data (IV rank) must be populated even when an event/earnings vetoes —
    the tile shows the data, not a null (operator: compute the variables even if blocked)."""
    c = derive_cost({"iv_term": _term((30, 0.55)), "event_within_hold": True})
    assert c.guard == "block"
    assert c.ivr == 55.0                          # IV rank still computed, not None
    e = derive_cost({"iv_term": _term((30, 0.55)), "days_to_earnings": 2})
    assert e.guard == "block" and e.ivr == 55.0


# ── honest-degrade: missing IV → caution (not silent ok) ──────────────────────
def test_missing_iv_is_caution_not_ok():
    c = derive_cost({})
    assert c.guard == "caution"
    assert c.provenance.quality == Quality.UNAVAILABLE


def test_cost_is_never_a_direction():
    """Structural guarantee: the Cost signal exposes no call/put field."""
    c = derive_cost({"iv_term": _term((30, 0.50))})
    assert not hasattr(c, "direction")


# ── golden: real interpolated-iv → normalize → derive ─────────────────────────
def test_golden_real_interpolated_iv():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = RawRecord(endpoint="/stock/SPY/interpolated-iv", params={}, ticker="SPY",
                    fetched_at="2026-06-08T15:05:00Z", content_hash="h", payload=payload)
    iv = normalize(raw)
    assert len(iv) >= 1
    c = derive_cost({"iv_term": iv})
    assert c.guard in ("ok", "caution", "block")
    assert c.ivr is not None and 0 <= c.ivr <= 100        # sane IV-rank value
