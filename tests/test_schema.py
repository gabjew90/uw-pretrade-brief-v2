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
