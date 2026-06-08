"""FlowAlert canonical-model contract tests (Phase 0).

These pin the validated shape Derive may rely on. Phase 2 ADDS a golden-bronze test
that validates a real captured flow-alerts row; these synthetic cases lock the
type/coercion/required-field behaviour now so the boundary can't drift silently.
"""
import pytest
from pydantic import ValidationError

from server.models import FlowAlert, Provenance, Quality


def _row(**over):
    base = dict(ticker="SPY", type="call", total_premium=40_500_000.0,
                created_at="2026-06-05T13:32:00Z")
    base.update(over)
    return base


def test_valid_row_builds():
    fa = FlowAlert(**_row())
    assert fa.ticker == "SPY"
    assert fa.type == "call"
    assert fa.total_premium == 40_500_000.0
    # optional, unconfirmed fields default to None (honest absence, not a fabricated 0)
    assert fa.volume_oi_ratio is None
    assert fa.has_sweep is None


def test_total_premium_string_is_coerced():
    """UW sends total_premium as a STRING — the boundary must coerce, not choke."""
    fa = FlowAlert(**_row(total_premium="40500000"))
    assert fa.total_premium == 40_500_000.0
    assert isinstance(fa.total_premium, float)


@pytest.mark.parametrize("raw,expected", [
    ("C", "call"), ("CALL", "call"), ("call", "call"),
    ("P", "put"), ("PUT", "put"), ("put", "put"),
])
def test_side_spellings_normalize(raw, expected):
    assert FlowAlert(**_row(type=raw)).type == expected


def test_unknown_side_fails_loudly():
    """A side we can't recognise must RAISE here — never be silently miscategorised."""
    with pytest.raises(ValidationError):
        FlowAlert(**_row(type="banana"))


def test_missing_required_premium_fails():
    bad = _row()
    del bad["total_premium"]
    with pytest.raises(ValidationError):
        FlowAlert(**bad)


def test_optional_fields_round_trip():
    fa = FlowAlert(**_row(volume_oi_ratio=2.3, strike=600.0, expiry="2026-06-12",
                          has_sweep=True))
    assert fa.volume_oi_ratio == 2.3
    assert fa.strike == 600.0
    assert fa.expiry == "2026-06-12"
    assert fa.has_sweep is True


def test_provenance_attaches():
    fa = FlowAlert(**_row(provenance=Provenance(quality=Quality.DEGRADED, note="stale")))
    assert fa.provenance.quality == Quality.DEGRADED
