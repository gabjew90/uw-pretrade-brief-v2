"""normalize_flow_alerts tests (Phase 3) — the detect-don't-trust boundary, exercised
against the REAL captured flow-alerts bronze (tests/fixtures/bronze/flow-alerts/SPY.json).
"""
import json
from pathlib import Path

import pytest

from server.models import Quality, Source
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import NormalizeError, normalize

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "flow-alerts" / "SPY.json"
ENDPOINT = "/option-trades/flow-alerts"


def _raw(payload, *, from_replay=False):
    return RawRecord(endpoint=ENDPOINT, params={}, ticker="SPY",
                     fetched_at="2026-06-08T15:05:00Z", content_hash="abc",
                     payload=payload, from_replay=from_replay)


def _golden():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_normalizes_real_bronze_to_flowalerts():
    alerts = normalize(_raw(_golden()))
    assert len(alerts) == 500                       # the captured pull was at the cap
    a0 = alerts[0]
    assert a0.ticker == "SPY"
    assert a0.type in ("call", "put")
    assert a0.total_premium > 0                      # sane non-None VALUE
    assert a0.volume_oi_ratio is not None and a0.volume_oi_ratio > 0


def test_provenance_live_from_fetched_at():
    a0 = normalize(_raw(_golden()))[0]
    assert a0.provenance.source == Source.LIVE
    assert a0.provenance.quality == Quality.REAL
    assert a0.provenance.as_of == "2026-06-08T15:05:00Z"


def test_provenance_archive_when_from_replay():
    a0 = normalize(_raw(_golden(), from_replay=True))[0]
    assert a0.provenance.source == Source.ARCHIVE


def test_truncation_flagged_at_cap():
    # the real fixture is exactly 500 rows → truncated
    assert normalize(_raw(_golden()))[0].truncated is True


def test_not_truncated_below_cap():
    payload = {"data": _golden()["data"][:10]}
    alerts = normalize(_raw(payload))
    assert len(alerts) == 10
    assert all(a.truncated is False for a in alerts)


def test_empty_response_is_legit_not_error():
    """A structurally valid empty response (pre-market, no alerts) → [], not a raise."""
    assert normalize(_raw({"data": []})) == []


def test_missing_volume_oi_ratio_raises_normalize_error():
    row = dict(_golden()["data"][0])
    row.pop("volume_oi_ratio", None)
    with pytest.raises(NormalizeError, match="volume_oi_ratio"):
        normalize(_raw({"data": [row]}))


def test_bad_side_raises_normalize_error_not_validation_error():
    row = dict(_golden()["data"][0])
    row["type"] = "banana"
    with pytest.raises(NormalizeError):       # typed, not a leaked pydantic ValidationError
        normalize(_raw({"data": [row]}))


def test_unregistered_endpoint_raises_not_implemented():
    unregistered = RawRecord(endpoint="/stock/SPY/made-up-endpoint", params={},
                             ticker="SPY", fetched_at="t", content_hash="h",
                             payload={"data": []})
    with pytest.raises(NotImplementedError):
        normalize(unregistered)
