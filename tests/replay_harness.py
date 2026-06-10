"""Shared REPLAY harness: seed a parquet lake with the committed golden bronze and run
the FULL build_view pipeline with live calls denied (governor REPLAY) and a frozen clock.
Used by tests/test_replay_parity.py and scripts/capture_golden_vm.py so the parity test
and the committed golden ViewModel can never drift apart in setup.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 6, 8, 15, 5, tzinfo=timezone.utc)
FIXED_ASOF = "2026-06-08"


def seed_lake(roots: dict) -> None:
    """Write the golden flow-alerts payload into the bronze layout ingest's fallback
    reads (exact params_json the orchestrator uses)."""
    from server.services import storage
    payload = json.loads((FIXTURES / "bronze" / "flow-alerts" / "SPY.json")
                         .read_text(encoding="utf-8"))
    storage.write_rows("bronze", "option-trades_flow-alerts", [{
        "endpoint": "/option-trades/flow-alerts",
        "params_json": json.dumps({"limit": 500, "ticker_symbol": "SPY"}, sort_keys=True),
        "fetched_at": "2026-06-08T15:05:00Z", "content_hash": "golden",
        "response": json.dumps(payload),
    }], ticker="SPY", dt="2026-06-08")


def build_replay_vm():
    """Run build_view('SPY') in REPLAY (governor denies all live calls → ingest reads the
    seeded bronze; everything unseeded degrades honestly) with the clock frozen."""
    from server.pipeline.orchestrate import build_view
    from server.services.governor import governor
    prev = governor.replay
    governor.replay = True
    try:
        return build_view("SPY", asof=FIXED_ASOF, now=FIXED_NOW)
    finally:
        governor.replay = prev
