"""Stage 1 — INGEST → immutable raw log.

Fetch a UW endpoint (governor-gated) and store the response VERBATIM in bronze with
metadata (endpoint, params, tier, fetched_at, content hash). No transformation. One
immutable file per fetch. In REPLAY the governor denies live calls and we read the
latest bronze part instead — same code path, so replay exercises everything downstream.

Contract:  ingest(endpoint, params, ticker?, priority) -> RawRecord
The RawRecord is what Normalize consumes — nothing parses bronze except Normalize.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from server.services import storage
from server.services.governor import Priority
from server.services.uw_client import UWError, get


@dataclass
class RawRecord:
    endpoint: str
    params: dict
    ticker: str | None
    fetched_at: str
    content_hash: str
    payload: dict          # the verbatim UW json
    from_replay: bool = False


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def ingest(endpoint: str, params: dict | None = None, *, ticker: str | None = None,
           priority: Priority = Priority.NORMAL) -> RawRecord:
    """Fetch + persist verbatim, or (REPLAY / over-budget / failure) read the latest
    bronze part. Raises UWError only when neither live NOR bronze is available."""
    params = params or {}
    try:
        resp = get(endpoint, params, priority=priority)
        rec = RawRecord(endpoint, params, ticker, resp.fetched_at, _hash(resp.json), resp.json)
        storage.write_rows("bronze", _ep_key(endpoint), [{
            "endpoint": endpoint, "params_json": json.dumps(params, sort_keys=True),
            "fetched_at": rec.fetched_at, "content_hash": rec.content_hash,
            "response": json.dumps(resp.json),
        }], ticker=ticker)
        return rec
    except UWError:
        return _read_bronze(endpoint, params, ticker)


def _ep_key(endpoint: str) -> str:
    # endpoint path → a filesystem-safe partition key (e.g. flow-alerts)
    return endpoint.strip("/").replace("/", "_")


def _read_bronze(endpoint: str, params: dict, ticker: str | None) -> RawRecord:
    rows = storage.read_endpoint("bronze", _ep_key(endpoint),
                                 where="params_json = ?", params=[json.dumps(params, sort_keys=True)],
                                 order_by="fetched_at DESC", limit=1)
    if not rows:
        raise UWError(f"no live + no bronze for {endpoint} {params}")
    row = rows[0]
    return RawRecord(endpoint, params, ticker, row.get("fetched_at", ""),
                     row.get("content_hash", ""), json.loads(row["response"]), from_replay=True)
