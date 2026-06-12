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
    assert r["overall"] in ("PERFECT", "NOT NOW")
    assert r["surfaces"]["direction"] == "PUTS"
    assert isinstance(r["caps"], list)             # gate-binding histogram input
    json.dumps(r)                                  # line-diffable: JSON-serializable


def test_summarize_base_rate_and_cap_histogram():
    """The 'is PERFECT reachable' instrument: base rate across ticker-days + which gate
    binds most. PERFECT firing daily means a threshold is wrong (acceptance #3); never
    firing means tune from THIS data, not vibes."""
    from scripts.backtest_replay import summarize
    rows = [
        {"overall": "PERFECT", "caps": []},
        {"overall": "NOT NOW", "caps": ["dealer_fuel", "cheap_vol"]},
        {"overall": "NOT NOW", "caps": ["dealer_fuel"]},
        {"overall": "NOT NOW", "caps": ["cost"]},
    ]
    s = summarize(rows)
    assert s["sessions"] == 4 and s["perfect"] == 1
    assert s["perfect_rate"] == 0.25
    assert s["cap_histogram"] == {"dealer_fuel": 2, "cheap_vol": 1, "cost": 1}
    assert summarize([])["perfect_rate"] is None       # empty archive stays honest


def test_outcome_join_scores_the_direction_call(lake):
    """Each row gains next-session move + called_right, from bronze stock-state (no UW)."""
    from scripts.backtest_replay import backtest
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    storage.write_rows("bronze", "option-trades_flow-alerts", [{
        "endpoint": "/option-trades/flow-alerts", "params_json": "{}",
        "fetched_at": "2026-06-08T15:05:00Z", "content_hash": "h",
        "response": json.dumps(payload)}], ticker="SPY", dt="2026-06-08")
    for dt, close in (("2026-06-08", 745.0), ("2026-06-09", 738.0)):   # -0.94% next day
        storage.write_rows("bronze", "stock_SPY_stock-state", [{
            "endpoint": "/stock/SPY/stock-state", "params_json": "{}",
            "fetched_at": f"{dt}T20:00:00Z", "content_hash": "h",
            "response": json.dumps({"data": {"close": str(close)}})}],
            ticker="SPY", dt=dt)
    r = backtest("SPY")[0]
    assert r["direction"] == "puts"
    assert r["outcome_date"] == "2026-06-09"
    assert r["outcome_pct"] == -0.94
    assert r["called_right"] is True               # puts + the market fell


def test_backtest_empty_lake_is_empty(lake):
    from scripts.backtest_replay import backtest
    assert backtest("SPY") == []


def test_backtest_diff_is_confined_to_the_changed_signal(lake, monkeypatch):
    """Diffability acceptance (ops-ci §7): an intentionally changed derive fn yields a
    non-empty diff CONFINED to the affected signal — proves a signal change's historical
    impact is observable from the JSONL alone."""
    from scripts.backtest_replay import backtest
    from server.pipeline import derive
    from server.models import Flow
    from server.services import provenance as prov

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    storage.write_rows("bronze", "option-trades_flow-alerts", [{
        "endpoint": "/option-trades/flow-alerts", "params_json": "{}",
        "fetched_at": "2026-06-08T15:05:00Z", "content_hash": "h",
        "response": json.dumps(payload),
    }], ticker="SPY", dt="2026-06-08")

    baseline = backtest("SPY")

    def flipped(canon, *, asof=None):              # the "changed signal"
        return Flow(direction="calls", direction_basis="opening_flow",
                    call_prem=1.0, put_prem=0.0, provenance=prov.derived())
    monkeypatch.setitem(derive.REGISTRY, "flow", flipped)
    changed = backtest("SPY")

    assert [r["date"] for r in baseline] == [r["date"] for r in changed]   # same sessions
    assert baseline[0]["direction"] == "puts" and changed[0]["direction"] == "calls"
    assert baseline[0]["surfaces"]["direction"] != changed[0]["surfaces"]["direction"]
    # confined: untouched signals read identically across the two runs
    for key in ("skew", "structural", "conviction"):
        assert baseline[0]["surfaces"].get(key) == changed[0]["surfaces"].get(key)


def test_backup_bronze_dry_run_and_tar(lake, tmp_path):
    """ops-ci §5 acceptance: dry-run exits 0; real run writes tar + sha256 manifest."""
    from scripts.backup_bronze import main
    src = lake["bronze"]
    out = tmp_path / "backups"
    assert main(["--src", str(src), "--out", str(out), "--dry-run"]) == 0
    assert not out.exists()                        # dry-run wrote nothing
    # seed one file so there is something to tar
    storage.write_rows("bronze", "option-trades_flow-alerts", [{
        "endpoint": "/x", "params_json": "{}", "fetched_at": "t",
        "content_hash": "h", "response": "{}"}], ticker="SPY", dt="2026-06-08")
    assert main(["--src", str(src), "--out", str(out)]) == 0
    tars = list(out.glob("bronze-*.tar.gz"))
    assert len(tars) == 1 and tars[0].stat().st_size > 0
    assert list(out.glob("bronze-*.sha256"))
