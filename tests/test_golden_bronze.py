"""Golden-bronze tests (Phase 2) — validate canonical models against REAL captured
UW payloads (tests/fixtures/bronze/), asserting a sane non-None VALUE (not just field
presence). These are the load-bearing offline tests: if UW changes a shape, they fail
here, not three layers downstream.

Captured 2026-06-08 (Mon, RTH) via scripts/capture_golden.py through the Railway bridge.
"""
import json
from pathlib import Path

import pytest

from server.models import FlowAlert

BRONZE = Path(__file__).parent / "fixtures" / "bronze"


def _rows(endpoint: str) -> list:
    payload = json.loads((BRONZE / endpoint / "SPY.json").read_text(encoding="utf-8"))
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    return data if isinstance(data, list) else [data]


def test_flow_alerts_fixture_exists_and_populated():
    rows = _rows("flow-alerts")
    assert len(rows) >= 100   # a real RTH SPY pull returns hundreds (capped at 500)


def test_flowalert_validates_real_bronze_row():
    """The Phase-0 contract holds against the REAL shape: type lowercase, premium/
    volume_oi_ratio/strike arrive as STRINGS and coerce, extras ignored."""
    row = _rows("flow-alerts")[0]
    fa = FlowAlert.model_validate(row)
    assert fa.ticker == "SPY"
    assert fa.type in ("call", "put")
    assert fa.total_premium > 0                    # sane non-None VALUE, not just presence
    assert fa.volume_oi_ratio is not None and fa.volume_oi_ratio > 0
    assert fa.created_at.endswith("Z") or "T" in fa.created_at


def test_flowalert_validates_every_row_in_fixture():
    """No row in a real 500-row pull breaks the contract (sign/shape drift guard)."""
    rows = _rows("flow-alerts")
    for row in rows:
        fa = FlowAlert.model_validate(row)
        assert fa.type in ("call", "put")
        assert fa.total_premium >= 0


def test_all_opening_trades_is_dead_use_volume_oi_ratio():
    """Live-confirmed: all_opening_trades is False even on clearly-opening trades
    (volume_oi_ratio >> 1). Direction MUST key on volume_oi_ratio, not this flag.
    Locks the v2 finding as a regression guard against re-introducing the dead proxy."""
    rows = _rows("flow-alerts")
    opening_by_voi = [r for r in rows if float(r.get("volume_oi_ratio") or 0) > 1]
    assert opening_by_voi, "expected some opening trades by volume_oi_ratio>1"
    # the dead flag does not light up even though these are opening
    assert all(r.get("all_opening_trades") is False for r in opening_by_voi)


@pytest.mark.parametrize("endpoint", [
    "flow-alerts", "greek-flow", "net-prem-ticks", "oi-per-strike",
])
def test_critical_fixtures_present(endpoint):
    """Every Phase-3 / cross-check critical endpoint has a captured fixture with rows."""
    assert _rows(endpoint), f"{endpoint} fixture empty"
