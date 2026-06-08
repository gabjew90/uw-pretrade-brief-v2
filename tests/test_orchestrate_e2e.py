"""End-to-end walking-skeleton tests (Phase 3): normalize → derive → decide → present
over a captured RawRecord, plus the honest-degrade path and REPLAY-reproducibility
(same RawRecord → identical ViewModel).
"""
import json
from pathlib import Path

import pytest

from server.models import ViewModel
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import NormalizeError
from server.pipeline.orchestrate import assemble, build_view

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "flow-alerts" / "SPY.json"


def _raw(payload):
    return RawRecord(endpoint="/option-trades/flow-alerts", params={}, ticker="SPY",
                     fetched_at="2026-06-08T15:05:00Z", content_hash="h", payload=payload)


def _golden_raw():
    return _raw(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_e2e_real_bronze_produces_verdict_and_direction_element():
    vm = assemble("SPY", _golden_raw(), asof="2026-06-08")
    assert isinstance(vm, ViewModel)
    assert vm.ticker == "SPY"
    # real SPY 6/8 is net put-side on opening flow; with only flow available (other
    # signals' inputs not in this canon) cost is caution → not Favorable → Mixed
    assert vm.verdict.direction == "puts"
    assert vm.verdict.overall == "Mixed"
    assert "flow" in vm.verdict.signals_used
    direction = next(e for e in vm.elements if e.key == "direction")
    assert direction.surface == "PUTS"
    assert direction.detail["basis"] == "opening_flow"
    assert direction.detail["put_premium"] > direction.detail["call_premium"]


def test_e2e_truncation_element_emitted_at_cap():
    vm = assemble("SPY", _golden_raw(), asof="2026-06-08")          # fixture is 500 rows
    assert any(e.key == "flow_truncation" and e.tone == "cautionary" for e in vm.elements)


def test_e2e_as_of_is_oldest_signal_provenance():
    vm = assemble("SPY", _golden_raw(), asof="2026-06-08")
    assert vm.as_of == "2026-06-08T15:05:00Z"     # from the flow provenance, not the arg


def test_replay_reproducible_identical_viewmodel():
    """Same RawRecord → byte-identical ViewModel on every run (REPLAY guarantee)."""
    a = assemble("SPY", _golden_raw(), asof="2026-06-08").model_dump()
    b = assemble("SPY", _golden_raw(), asof="2026-06-08").model_dump()
    assert a == b


def test_honest_degrade_empty_flow_is_stand_down_not_guess():
    vm = assemble("SPY", _raw({"data": []}), asof="2026-06-08")
    assert vm.verdict.action == "Stand down"
    direction = next(e for e in vm.elements if e.key == "direction")
    assert direction.surface is None
    assert direction.tone == "unavailable"
    assert "reason" in direction.detail


def test_malformed_row_does_not_leak_validation_error():
    bad = dict(json.loads(FIXTURE.read_text(encoding="utf-8"))["data"][0])
    bad["type"] = "banana"
    with pytest.raises(NormalizeError):       # typed boundary failure, surfaced
        assemble("SPY", _raw({"data": [bad]}), asof="2026-06-08")


def test_build_view_degrades_when_ingest_fails(monkeypatch):
    """When ingest raises (no live + no bronze), build_view returns a well-formed
    unavailable ViewModel — never a crash, never a guessed direction."""
    from server.pipeline import orchestrate
    from server.services.uw_client import UWError

    def _boom(*a, **k):
        raise UWError("no live + no bronze")
    monkeypatch.setattr(orchestrate, "ingest", _boom)

    vm = build_view("SPY", asof="2026-06-08")
    assert vm.verdict.action == "Stand down"
    assert next(e for e in vm.elements if e.key == "direction").tone == "unavailable"


def test_http_api_view_serializes_cleanly(monkeypatch):
    """GET /api/view/SPY returns a JSON ViewModel with ONLY Element + Verdict shapes —
    no Signal / RawRecord / FlowAlert leaks into the response (the one rule, enforced)."""
    from fastapi.testclient import TestClient

    from server.pipeline import orchestrate
    from server import main
    monkeypatch.setattr(main, "build_view",
                        lambda t: assemble(t, _golden_raw(), asof="2026-06-08"))

    with TestClient(main.app) as client:
        r = client.get("/api/view/SPY")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "SPY"
    assert body["verdict"]["direction"] == "puts"
    assert body["verdict"]["overall"] in ("Favorable", "Mixed", "Stand down")
    assert {e["key"] for e in body["elements"]} >= {"direction", "flow_truncation"}
    # the response carries only view-model keys — no raw signal/canonical fields
    direction = next(e for e in body["elements"] if e["key"] == "direction")
    assert set(direction) == {"key", "label", "surface", "detail", "provenance", "tone"}
