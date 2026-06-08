"""Capture verbatim golden bronze fixtures (v3 Phase 2).

Hits each UW endpoint once via `uw_client.get` and writes the raw JSON VERBATIM to
`tests/fixtures/bronze/<endpoint>/<ticker>.json`, with a `_meta` sidecar
`{fetched_at, path, params, status, content_hash, row_count}`. Writes are atomic
(temp→os.replace). These committed fixtures are the offline ground truth for all
normalize + derive tests — assumptions about field shapes are killed here, not downstream.

Run via the Railway bridge (prod UW_API_KEY injected; script runs locally + writes the
repo's fixtures):

    $env:RAILWAY_API_TOKEN = [Environment]::GetEnvironmentVariable("RAILWAY_API_TOKEN","User")
    railway run python scripts/capture_golden.py SPY
    railway run python scripts/capture_golden.py SPY --only flow-alerts greek-flow
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root on path

from scripts.uw_probe_targets import build_targets, make_getj, resolve_expiries, unwrap_rows  # noqa: E402
from server.services.uw_client import UWError, get  # noqa: E402

getj = make_getj(get)
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bronze"


def _hash(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    args = [a for a in sys.argv[1:]]
    only: list[str] = []
    if "--only" in args:
        i = args.index("--only")
        only = [a.lower() for a in args[i + 1:]]
        args = args[:i]
    ticker = (args[0] if args else "SPY").upper()

    near, far = resolve_expiries(ticker, getj)
    targets = build_targets(ticker, near, far)
    if only:
        targets = [t for t in targets if t.label.lower() in only]

    print(f"=== capture golden bronze — ticker={ticker}  near={near}  ~30d={far} ===")
    written = failed = 0
    for tgt in targets:
        try:
            resp = getj(tgt.path, tgt.params or None)
        except (UWError, ValueError) as e:
            print(f"  FAIL  {tgt.label:32} {e}")
            failed += 1
            continue
        rows = unwrap_rows(resp)
        ep_dir = OUT / tgt.label
        _atomic_write(ep_dir / f"{ticker}.json", json.dumps(resp, indent=2, default=str))
        _atomic_write(ep_dir / f"{ticker}._meta.json", json.dumps({
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
            "path": tgt.path, "params": tgt.params, "row_count": len(rows),
            "content_hash": _hash(resp), "critical": tgt.critical,
        }, indent=2))
        print(f"  OK    {tgt.label:32} rows={len(rows)} -> {ep_dir.relative_to(OUT.parent.parent)}")
        written += 1

    print(f"--- captured {written} fixture(s), {failed} failure(s) ---")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
