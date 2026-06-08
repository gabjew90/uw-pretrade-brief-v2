"""Smoke tests — the scaffold wires end-to-end and the core contracts hold."""
from server.models import Provenance, Quality, Source, ViewModel
from server.pipeline.decide import decide
from server.pipeline.derive import derive_all
from server.pipeline.present import present


def test_pipeline_runs_end_to_end_empty():
    """derive→decide→present produces a well-formed (empty) view model with no signals
    registered yet — proves the boundaries connect."""
    signals = derive_all({}, asof="2026-06-05")
    verdict = decide(signals)
    vm = present("SPY", signals, verdict, as_of="2026-06-05")
    assert isinstance(vm, ViewModel)
    assert vm.ticker == "SPY"
    assert vm.verdict is not None
    assert vm.verdict.signals_used == []   # none registered → visibly empty, not silent


def test_provenance_worst_case_merge():
    a = Provenance(source=Source.LIVE, quality=Quality.REAL, as_of="2026-06-05T16:00:00Z")
    b = Provenance(source=Source.ARCHIVE, quality=Quality.DEGRADED, as_of="2026-06-04T16:00:00Z")
    w = Provenance.worst(a, b)
    assert w.quality == Quality.DEGRADED          # worst quality wins
    assert w.source == Source.ARCHIVE             # worst source wins
    assert w.as_of == "2026-06-04T16:00:00Z"      # oldest input wins


def test_app_imports_and_health_shape():
    from server.main import app, health
    assert app.title == "UW Pretrade Brief v3"
    h = health()
    assert h["ok"] is True and "phase" in h and "oi_settled_through" in h
    # governor snapshot is surfaced on /health (budget visibility — the 2026-05-29 lesson)
    assert "budget" in h
    assert {"calls_today", "daily_cap", "source"} <= set(h["budget"])
