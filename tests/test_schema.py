"""Tests for the Pydantic snapshot schema."""
from __future__ import annotations
from datetime import datetime, timezone
import pytest

from server.schema import Row, Snapshot, Regime, Gates, GateMethod, Flow, OI, OIStrike, DarkPool


def test_minimal_row_validates():
    Row(
        ticker="NVDA",
        spot=154.20,
        direction="calls",
        gates=Gates(flow="green", oi="green", structural="green", cost="green"),
        gate_method=GateMethod(flow="cross_sectional", oi="absolute",
                                structural="absolute", cost="percentile"),
    )


def test_snapshot_serializes_to_dict_with_iso_timestamps():
    snap = Snapshot(
        fetched_at=datetime(2026, 5, 27, 14, 16, tzinfo=timezone.utc),
        regime=Regime(label="normal", detail="VIX 18.4", vix=18.4),
        rows=[],
    )
    d = snap.model_dump(mode="json")
    assert d["fetched_at"].startswith("2026-05-27T14:16")
    assert d["regime"]["label"] == "normal"


def test_row_accepts_optional_failures_list():
    r = Row(
        ticker="NVDA",
        spot=154.20,
        direction="calls",
        gates=Gates(flow="green", oi="green", structural="green", cost="green"),
        gate_method=GateMethod(flow="cross_sectional", oi="absolute",
                                structural="absolute", cost="percentile"),
        _failures=["darkpool", "earnings"],
    )
    assert r._failures == ["darkpool", "earnings"]


def test_row_has_direction_basis_default():
    from server.schema import Row, Gates, GateMethod
    row = Row(ticker="SPY", spot=1.0, direction="calls",
              gates=Gates(flow="green", oi="green", structural="green", cost="green"),
              gate_method=GateMethod(flow="absolute", oi="absolute",
                                     structural="absolute", cost="absolute"))
    assert row.direction_basis == "gamma_fallback"           # honest default
    row2 = Row(ticker="SPY", spot=1.0, direction="calls", direction_basis="opening_flow",
               gates=row.gates, gate_method=row.gate_method)
    assert row2.direction_basis == "opening_flow"


def test_verdict_model_and_row_field_default_none():
    from server.schema import Verdict, Row, Gates, GateMethod
    v = Verdict(positioning="green", structural="green", skew="agree",
                cost_guard="ok", overall="Favorable", action="Worth acting on")
    assert v.signal_conflict is False and v.conflict_legs == [] and v.rr25 is None
    r = Row(ticker="AAPL", spot=1.0, direction="calls",
            gates=Gates(flow="green", oi="green", structural="green", cost="green"),
            gate_method=GateMethod(flow="absolute", oi="absolute", structural="absolute", cost="absolute"))
    assert r.verdict is None


def test_row_is_light_defaults_false():
    from server.schema import Row, Gates, GateMethod
    r = Row(ticker="AAPL", spot=1.0, direction="calls",
            gates=Gates(flow="yellow", oi="yellow", structural="yellow", cost="yellow"),
            gate_method=GateMethod(flow="cross_sectional", oi="absolute",
                                   structural="absolute", cost="percentile"))
    assert r.is_light is False


def test_regime_has_structured_market_fields():
    from server.schema import Regime
    r = Regime(label="normal", headline="Trend regime — extends", posture="Favorable")
    assert r.posture == "Favorable"
    assert r.headline.startswith("Trend")
    assert r.event_line is None and r.opex is False        # honest defaults
    assert r.vix == 0.0 and r.detail == ""                 # back-compat fields intact
