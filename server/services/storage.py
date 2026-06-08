"""Storage — append-only parquet, three tiers, DuckDB read layer.

  bronze/  raw UW responses, verbatim, one immutable file per fetch (Ingest writes)
  silver/  canonical typed records (Normalize writes)
  gold/    derived signals (Derive writes)

Invariants (these delete whole v2 bug classes):
  * WRITES ARE APPEND-ONLY + ATOMIC: temp file → os.replace. Never read-modify-write.
  * READS GO THROUGH DUCKDB: SQL over the parquet partitions; you query, never mutate.
  * BRONZE IS IMMUTABLE: re-deriving silver/gold from bronze = a free backtest harness.

Partition layout (Hive-style so DuckDB can prune):
  <tier>/endpoint=<ep>/dt=<YYYY-MM-DD>/ticker=<T>/part-<HHMMSS>-<hex>.parquet

This is the storage SKELETON: the write/read primitives + layout are real; the
per-endpoint schemas land with Normalize per instructions. Compaction is a separate
cron (not here) — runtime only ever appends + queries.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from server.config import settings

Tier = str  # "bronze" | "silver" | "gold"


def _tier_root(tier: Tier) -> Path:
    return {"bronze": settings.bronze, "silver": settings.silver, "gold": settings.gold}[tier]


def _partition_dir(tier: Tier, endpoint: str, dt: str, ticker: str | None) -> Path:
    p = _tier_root(tier) / f"endpoint={endpoint}" / f"dt={dt}"
    if ticker:
        p = p / f"ticker={ticker.upper()}"
    return p


def write_part(tier: Tier, endpoint: str, table: pa.Table, *,
               ticker: str | None = None, dt: str | None = None) -> Path:
    """Append one immutable part file (atomic temp→os.replace). Returns its path."""
    now = datetime.now(timezone.utc)
    dt = dt or now.strftime("%Y-%m-%d")
    d = _partition_dir(tier, endpoint, dt, ticker)
    d.mkdir(parents=True, exist_ok=True)
    name = f"part-{now.strftime('%H%M%S')}-{secrets.token_hex(4)}.parquet"
    final = d / name
    tmp = d / (name + ".tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, final)   # atomic on the same filesystem
    return final


def write_rows(tier: Tier, endpoint: str, rows: list[dict[str, Any]], **kw) -> Path | None:
    """Convenience: write a list of dict rows as a parquet part. No-op on empty."""
    if not rows:
        return None
    return write_part(tier, endpoint, pa.Table.from_pylist(rows), **kw)


def _glob(tier: Tier, endpoint: str) -> str:
    # DuckDB reads every part across all partitions; Hive types give it dt/ticker cols.
    return str(_tier_root(tier) / f"endpoint={endpoint}" / "**" / "*.parquet")


def query(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """Run read-only SQL (DuckDB) over the parquet lake. Use `read('<tier>','<ep>')`
    inside SQL via the `{src}` placeholder, or call `read_endpoint` for the common case.
    Returns a list of dict rows. NEVER mutates anything."""
    con = duckdb.connect(database=":memory:")
    try:
        cur = con.execute(sql, params or [])
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def read_endpoint(tier: Tier, endpoint: str, *, where: str = "", params: list[Any] | None = None,
                  order_by: str = "", limit: int | None = None) -> list[dict[str, Any]]:
    """Read all parts for one endpoint with optional filter/order/limit. Returns [] when
    the partition doesn't exist yet (cold start) rather than raising."""
    root = _tier_root(tier) / f"endpoint={endpoint}"
    if not root.is_dir():
        return []
    sql = f"SELECT * FROM read_parquet('{_glob(tier, endpoint)}', hive_partitioning=true)"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    try:
        return query(sql, params)
    except duckdb.Error:
        return []   # no files matched the glob yet
