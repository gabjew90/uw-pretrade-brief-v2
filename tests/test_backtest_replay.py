"""Backtest harness test (item 5c, ops-ci spec §7) — seed a tmp bronze lake with the real
golden flow-alerts payload, re-derive, and assert a line-diffable signal-history row comes
back with the known direction. Zero network.
"""
import json
from pathlib import Path

import pytest

from server.services import storage

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "flow-alerts" / "SPY.json"


@pytest.fixture
def lake(tmp_path, monkeypatch):
    roots = {"bronze": tmp_path / "bronze", "silver": tmp_path / "silver",
             "gold": tmp_path / "gold"}
    monkeypatch.setattr(storage, "_tier_root", lambda tier: roots[tier])
    return roots


def test_backtest_rederives_a_session_from_bronze(lake):
    from scripts.backtest_replay import backtest

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    storage.write_rows("bronze", "option-trades_flow-alerts", [{
        "endpoint": "/option-trades/flow-alerts", "params_json": "{}",
        "fetched_at": "2026-06-08T15:05:00Z", "content_hash": "h",
        "response": json.dumps(payload),
    }], ticker="SPY", dt="2026-06-08")

    rows = backtest("SPY")
    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2026-06-08" and r["ticker"] == "SPY"
    assert r["direction"] == "puts"               # the known golden read (session-filtered)
    assert r["overall"] in ("Favorable", "Mixed", "Stand down")
    assert r["surfaces"]["direction"] == "PUTS"
    json.dumps(r)                                  # line-diffable: JSON-serializable


def test_backtest_empty_lake_is_empty(lake):
    from scripts.backtest_replay import backtest
    assert backtest("SPY") == []
