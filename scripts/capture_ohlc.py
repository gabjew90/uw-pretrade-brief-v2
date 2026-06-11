"""Probe + capture 15-minute OHLC candles (price axis for the design phase).

    railway run uv run python scripts/capture_ohlc.py SPY 15m
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.services.uw_client import UWError, get  # noqa: E402


def main() -> int:
    t = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    size = sys.argv[2] if len(sys.argv) > 2 else "15m"
    try:
        r = get(f"/stock/{t}/ohlc/{size}").json
    except (UWError, ValueError) as e:
        print("ERR:", e)
        return 1
    rows = r.get("data", r) if isinstance(r, dict) else r
    if not rows:
        print("empty payload:", json.dumps(r)[:200])
        return 1
    print("rows:", len(rows))
    print("keys:", ", ".join(sorted(rows[0].keys())))
    print("newest:", json.dumps(rows[0])[:280])
    print("oldest:", json.dumps(rows[-1])[:200])
    out = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bronze" / f"ohlc-{size}" / f"{t}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    print("saved:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
