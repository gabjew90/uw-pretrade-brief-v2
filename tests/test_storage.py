"""Storage tests (Phase 1) — the append-only parquet + DuckDB read layer.

These assert the invariants that delete v2 bug classes:
  * write→read round-trips through DuckDB,
  * writes are ATOMIC (no .tmp leftover) and APPEND-ONLY (two writes = two parts),
  * a cold partition reads as [] (never raises),
  * the where/order/limit path ingest relies on works.

Isolated from real config by pointing the tier roots at a tmp dir.
"""
import pytest

from server.services import storage


@pytest.fixture
def lake(tmp_path, monkeypatch):
    roots = {"bronze": tmp_path / "bronze",
             "silver": tmp_path / "silver",
             "gold": tmp_path / "gold"}
    monkeypatch.setattr(storage, "_tier_root", lambda tier: roots[tier])
    return roots


def test_write_then_read_round_trips(lake):
    storage.write_rows("bronze", "flow-alerts",
                       [{"endpoint": "/x", "fetched_at": "2026-06-05T00:00:00Z", "response": "{}"}],
                       ticker="SPY")
    rows = storage.read_endpoint("bronze", "flow-alerts")
    assert len(rows) == 1
    assert rows[0]["fetched_at"] == "2026-06-05T00:00:00Z"
    # Hive partition columns are recoverable from the path.
    assert rows[0]["ticker"] == "SPY"


def test_append_only_two_writes_two_parts(lake):
    storage.write_rows("bronze", "flow-alerts",
                       [{"fetched_at": "t1", "response": "a"}], ticker="SPY", dt="2026-06-05")
    storage.write_rows("bronze", "flow-alerts",
                       [{"fetched_at": "t2", "response": "b"}], ticker="SPY", dt="2026-06-05")
    rows = storage.read_endpoint("bronze", "flow-alerts", order_by="fetched_at")
    assert [r["fetched_at"] for r in rows] == ["t1", "t2"]   # both survive — nothing overwritten
    # Two physical part files exist in the partition (append-only, not read-modify-write).
    part_dir = lake["bronze"] / "endpoint=flow-alerts" / "dt=2026-06-05" / "ticker=SPY"
    parts = list(part_dir.glob("*.parquet"))
    assert len(parts) == 2


def test_write_is_atomic_no_tmp_left(lake):
    storage.write_rows("bronze", "flow-alerts",
                       [{"fetched_at": "t1", "response": "a"}], ticker="SPY", dt="2026-06-05")
    part_dir = lake["bronze"] / "endpoint=flow-alerts" / "dt=2026-06-05" / "ticker=SPY"
    assert list(part_dir.glob("*.parquet"))           # the final file landed
    assert not list(part_dir.glob("*.tmp"))           # no temp file leaked


def test_cold_partition_reads_empty_not_raises(lake):
    assert storage.read_endpoint("silver", "never-written") == []


def test_empty_rows_is_noop(lake):
    assert storage.write_rows("gold", "x", []) is None


def test_where_order_limit_path(lake):
    """The exact read shape ingest._read_bronze uses: filter by a column, newest first, limit 1."""
    for ts in ("t1", "t2", "t3"):
        storage.write_rows("bronze", "flow-alerts",
                           [{"params_json": "{}", "fetched_at": ts, "response": ts}],
                           ticker="SPY", dt="2026-06-05")
    rows = storage.read_endpoint("bronze", "flow-alerts",
                                 where="params_json = ?", params=["{}"],
                                 order_by="fetched_at DESC", limit=1)
    assert len(rows) == 1
    assert rows[0]["fetched_at"] == "t3"
