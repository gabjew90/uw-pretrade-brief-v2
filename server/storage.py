"""Storage layer: read-through cache in front of UW + parquet archive writer.

Layout on disk:
  $DATA_DIR/
    raw/
      endpoint=<name>/
        dt=YYYY-MM-DD/
          ticker=<TICKER>/        ← absent for cross-ticker endpoints
            part-HHMM.parquet     ← one file per hour, append-mode
    snapshots.jsonl
    sticky.json

Per-call write is best-effort: any I/O failure is logged and swallowed so the
dashboard continues to render — losing archive coverage is preferable to
crashing the snapshot pipeline.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def _partition_path(endpoint: str, ticker: str | None, fetched_at: datetime) -> Path:
    """Build the parquet partition path for this (endpoint, ticker, hour)."""
    dt = fetched_at.strftime("%Y-%m-%d")
    hhmm = fetched_at.strftime("%H") + "00"  # bucket to the hour
    parts = [_data_dir(), "raw", f"endpoint={endpoint}", f"dt={dt}"]
    if ticker:
        parts.append(f"ticker={ticker}")
    parts.append(f"part-{hhmm}.parquet")
    return Path(*parts)


def _record_schema() -> pa.Schema:
    # ticker is NOT written as a column — it is encoded in the partition path
    # (ticker=<TICKER>/) so pyarrow's Hive-partition inference doesn't conflict.
    return pa.schema([
        ("fetched_at", pa.timestamp("us", tz="UTC")),
        ("params_json", pa.string()),
        ("status_code", pa.int32()),
        ("latency_ms", pa.int32()),
        ("response", pa.string()),  # JSON-serialized; schema-on-read via duckdb
    ])


def write_response(
    endpoint: str,
    ticker: str | None,
    params: dict | None,
    response: Any,
    status_code: int,
    latency_ms: int,
    fetched_at: datetime,
) -> bool:
    """Append one row to the appropriate parquet partition.

    Returns True on success, False on any I/O failure (logged, not raised).
    """
    try:
        path = _partition_path(endpoint, ticker, fetched_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "fetched_at": [fetched_at],
            # ticker omitted: encoded in the partition path (ticker=<TICKER>/)
            "params_json": [json.dumps(params or {}, sort_keys=True)],
            "status_code": [status_code],
            "latency_ms": [latency_ms],
            "response": [json.dumps(response, default=str)],
        }
        new_table = pa.table(row, schema=_record_schema())
        if path.exists():
            existing = pq.ParquetFile(path).read()
            new_table = pa.concat_tables([existing, new_table])
        pq.write_table(new_table, path, compression="zstd", use_dictionary=False)
        return True
    except Exception as e:
        log.error("storage write failed: endpoint=%s ticker=%s err=%s", endpoint, ticker, e)
        return False
