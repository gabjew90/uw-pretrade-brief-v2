"""Smoke tests — the scaffold wires end-to-end and the core contracts hold."""
from server.models import Provenance, Quality, Source, ViewModel
from server.pipeline.decide import decide
from server.pipeline.derive import derive_all
from server.pipeline.present import present


def test_pipeline_runs_end_to_end_empty():
    """derive→decide→present produces a well-formed view model on EMPTY canonical input
    — proves the boundaries connect and that a signal with no inputs degrades to
    `unavailable` (never a fabricated value)."""
    signals = derive_all({}, asof="2026-06-05")
    verdict = decide(signals)
    vm = present("SPY", signals, verdict, as_of="2026-06-05")
    assert isinstance(vm, ViewModel)
    assert vm.ticker == "SPY"
    assert vm.verdict is not None
    # `flow` is registered (Phase 3); with no canonical input it is unavailable, not guessed
    assert "flow" in signals
    assert signals["flow"].direction is None
    assert signals["flow"].provenance.quality.value == "unavailable"
    assert "flow" in vm.verdict.signals_used     # consumed by name, visibly
    assert vm.verdict.overall == "NOT NOW"       # empty inputs can never read PERFECT


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
