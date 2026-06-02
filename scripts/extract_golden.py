#!/usr/bin/env python3
"""Extract one real golden payload per UW endpoint from the captured archive into
tests/fixtures/golden/<endpoint>.json, so the schema-contract tests validate
against REAL responses (not hand-made assumptions).

Run once after pulling the prod archive (scripts/pull_archive.py):
    DATA_DIR=./data python scripts/extract_golden.py

Picks, for each endpoint, the most recent non-empty response (the one with the
most rows, to maximize field coverage). Commits the fixtures.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

import pyarrow.parquet as pq

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "golden"

# Endpoints whose field shapes the code depends on (the contract surface).
ENDPOINTS = [
    "spot_exposures_strike", "oi_per_strike", "greeks", "atm_chains",
    "option_contracts", "volatility_term_structure", "interpolated_iv",
    "greek_exposure_expiry", "spot_exposures_expiry_strike", "earnings",
    "darkpool", "flow_alerts",
]


def _best_payload(endpoint: str):
    """Scan all parquet for this endpoint; return the response payload with the
    most data rows (best field coverage)."""
    base = DATA_DIR / "raw" / f"endpoint={endpoint}"
    if not base.is_dir():
        return None
    best, best_n = None, -1
    for path in base.rglob("*.parquet"):
        try:
            table = pq.read_table(path, columns=["response"])
        except Exception:
            continue
        for cell in table.column("response").to_pylist():
            try:
                payload = json.loads(cell)
            except (TypeError, json.JSONDecodeError):
                continue
            rows = payload.get("data") if isinstance(payload, dict) else payload
            n = len(rows) if isinstance(rows, list) else 0
            if n > best_n:
                best, best_n = payload, n
    return best


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    written = 0
    for ep in ENDPOINTS:
        payload = _best_payload(ep)
        if payload is None:
            print(f"  SKIP {ep}: no archive data")
            continue
        rows = payload.get("data") if isinstance(payload, dict) else payload
        n = len(rows) if isinstance(rows, list) else 0
        (OUT / f"{ep}.json").write_text(json.dumps(payload, indent=2, default=str),
                                        encoding="utf-8")
        print(f"  wrote {ep}.json ({n} rows)")
        written += 1
    print(f"\n{written}/{len(ENDPOINTS)} golden fixtures written to {OUT}")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
