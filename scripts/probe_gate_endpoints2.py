"""Follow-up probe: ohlc/1d depth (lowercase) + atm-chains with expirations[] param.
Run via railway run. ASCII-only."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

BASE = "https://api.unusualwhales.com/api"
KEY = os.environ.get("UW_API_KEY", "")
OUT = Path("data/probe")
OUT.mkdir(parents=True, exist_ok=True)


def get(name, url, params):
    try:
        r = requests.get(url, params=params,
                         headers={"Authorization": f"Bearer {KEY}"}, timeout=30)
    except requests.RequestException as e:
        print(f"  {name}: EXC {e}")
        return
    if r.status_code != 200:
        print(f"  {name}: HTTP {r.status_code}  {r.text[:200]}")
        return
    j = r.json()
    data = j.get("data", j)
    rows = data if isinstance(data, list) else [data]
    keys = sorted(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
    print(f"  {name}: 200 rows={len(rows)} keys={keys[:16]}")
    if rows and isinstance(rows[0], dict):
        print(f"    first: {json.dumps(rows[0], default=str)[:220]}")
        print(f"    last:  {json.dumps(rows[-1], default=str)[:220]}")
    OUT.joinpath(f"{name}.json").write_text(json.dumps(j), encoding="utf-8")


if __name__ == "__main__":
    if not KEY:
        print("UW_API_KEY not set"); sys.exit(1)
    print("=== ohlc 1d depth ===")
    get("ohlc_1d_TSLA", f"{BASE}/stock/TSLA/ohlc/1d", {"limit": 2500})
    print("=== atm-chains with expirations ===")
    get("atm_chains_TSLA", f"{BASE}/stock/TSLA/atm-chains",
        {"expirations[]": "2026-07-24"})
