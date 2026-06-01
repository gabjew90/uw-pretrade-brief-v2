#!/usr/bin/env python3
"""Pull the production data archive for local replay/dev.

Downloads the prod DATA_DIR (snapshots.jsonl + recent parquet) via the
token-guarded /admin/export endpoint and unpacks it into ./data, so you can run
the whole dashboard offline against REAL captured data — no UW key, no market
hours, no rate limits:

    python scripts/pull_archive.py --token "$BACKFILL_TOKEN"
    DATA_DIR=./data REPLAY=1 uvicorn server.main:app --port 8000

Then open http://localhost:8000 and click any ticker — all tiles (including the
Tile 3 gamma map and the Tile 4 contract picker) render from the captured
archive.
"""
from __future__ import annotations
import argparse
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://uw-pretrade-brief-v2-production.up.railway.app"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--token", required=True, help="BACKFILL_TOKEN value")
    ap.add_argument("--url", default=DEFAULT_URL, help="base URL of the deployed app")
    ap.add_argument("--days", type=int, default=7, help="recent parquet days to include")
    ap.add_argument("--dest", default="./data", help="local DATA_DIR to unpack into")
    args = ap.parse_args()

    url = f"{args.url}/admin/export?token={args.token}&days={args.days}"
    print(f"downloading archive from {args.url} (last {args.days} days)…")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            blob = resp.read()
    except Exception as e:
        print(f"download failed: {e}", file=sys.stderr)
        return 1

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        tar.extractall(dest)
    n = sum(1 for _ in dest.rglob("*") if _.is_file())
    print(f"unpacked {len(blob)//1024} KiB → {dest} ({n} files)")
    print(f"\nrun the dashboard offline:\n"
          f"  DATA_DIR={dest} REPLAY=1 uvicorn server.main:app --port 8000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
