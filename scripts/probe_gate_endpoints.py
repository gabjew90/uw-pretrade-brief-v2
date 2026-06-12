"""Probe the strict-conjunction directive's tier-unverified endpoints (run via
`railway run python scripts/probe_gate_endpoints.py` so UW_API_KEY is injected).

Decides which gates exist v1 vs are born DARK:
  P2 shorts volume-and-ratio, P3 ftds + interest-float (squeeze trap),
  C1 atm-chains + earnings history depth (implied vs historical earnings move),
  G4 ohlc/1D depth (ADV normalization).
ASCII-only prints (cp1252 console). Saves 200-payloads to data/probe/ for fixtures.
"""
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

PROBES = [
    ("shorts_volume_ratio", "/shorts/{t}/volume-and-ratio", {}),
    ("shorts_ftds", "/shorts/{t}/ftds", {}),
    ("shorts_interest_float", "/shorts/{t}/interest-float", {}),
    ("atm_chains", "/stock/{t}/atm-chains", {}),
    ("ohlc_1d", "/stock/{t}/ohlc/1D", {}),
    ("earnings", "/stock/{t}/earnings", {}),
]


def probe(ticker: str) -> None:
    print(f"=== {ticker} ===")
    for name, path, params in PROBES:
        url = BASE + path.format(t=ticker)
        try:
            r = requests.get(url, params=params,
                             headers={"Authorization": f"Bearer {KEY}"}, timeout=30)
        except requests.RequestException as e:
            print(f"  {name}: EXC {e}")
            continue
        body = r.text[:200].replace("\n", " ")
        if r.status_code != 200:
            print(f"  {name}: HTTP {r.status_code}  {body}")
            continue
        try:
            j = r.json()
        except ValueError:
            print(f"  {name}: 200 NON-JSON  {body}")
            continue
        data = j.get("data", j)
        rows = data if isinstance(data, list) else [data]
        keys = sorted(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
        print(f"  {name}: 200  rows={len(rows)}  keys={keys[:14]}")
        if rows and isinstance(rows[0], dict):
            print(f"    sample: {json.dumps(rows[0], default=str)[:240]}")
        if len(rows) > 1 and isinstance(rows[-1], dict):
            print(f"    last:   {json.dumps(rows[-1], default=str)[:160]}")
        (OUT / f"{name}_{ticker}.json").write_text(json.dumps(j), encoding="utf-8")


if __name__ == "__main__":
    if not KEY:
        print("UW_API_KEY not set"); sys.exit(1)
    for t in sys.argv[1:] or ["SPY", "TSLA"]:
        probe(t)
