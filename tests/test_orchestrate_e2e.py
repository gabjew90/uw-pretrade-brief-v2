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
    assert direction.meaning                          # plain-English read present
    assert direction.detail["Read from"] == "opening flow"


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
    assert "why" in direction.detail


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


def test_one_side_picker_verdict_cost_oi_all_agree():
    """Fix 2 regression: when opening flow (put) and total flow (call) split, the verdict
    side, the cost contract, and the OI cluster must ALL be the opening side (put)."""
    from datetime import date
    from server.models import FlowAlert, IVTermPoint, OISnapshot, OptionContract
    from server.pipeline.decide import decide
    from server.pipeline.derive import derive_all, flow_side
    from server.pipeline.orchestrate import _flow_cluster

    asof, asof_d = "2026-06-08", date(2026, 6, 8)
    alerts = [  # opening (voi>1) = PUT; a bigger CLOSING (voi<=1) call must not flip the side
        FlowAlert(ticker="SPY", type="put", total_premium=1_000_000, volume_oi_ratio=5.0,
                  created_at="t", strike=600, expiry="2026-06-12"),
        FlowAlert(ticker="SPY", type="call", total_premium=9_000_000, volume_oi_ratio=0.3,
                  created_at="t", strike=610, expiry="2026-06-12"),
    ]
    side, _ = flow_side(alerts)
    assert side == "put"
    canon = {
        "flow_alerts": alerts, "flow_side": side,
        "flow_strikes": _flow_cluster(alerts, side, asof_d), "spot": 600.0,
        "option_contracts": [OptionContract(type="put", strike=600, expiry="2026-06-10", bid=1.0, ask=1.05),
                             OptionContract(type="call", strike=610, expiry="2026-06-10", bid=1.0, ask=1.05)],
        "iv_term": [IVTermPoint(date=asof, days=4, percentile=0.4, implied_move_perc=0.05)],
        "oi_sessions": [[OISnapshot(date="2026-06-05", strike=600, put_oi=1000)],
                        [OISnapshot(date="2026-06-06", strike=600, put_oi=1300)]],
    }
    signals = derive_all(canon, asof=asof)
    verdict = decide(signals)
    assert verdict.direction == "puts"                       # verdict side
    assert signals["cost"].contract["type"] == "put"          # cost prices a PUT
    assert signals["positioning"].side == "put"               # OI watches the PUT side


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
    assert set(direction) == {"key", "label", "surface", "meaning", "logic", "detail",
                              "series", "provenance", "tone"}
    # chart-ready series for the future UI: top strikes by premium, per side
    assert direction["series"]["kind"] == "strike_bars"
    assert direction["series"]["points"], "top-strike chart rows must be populated"
