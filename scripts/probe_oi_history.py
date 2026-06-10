"""Probe: how deep does per-contract OI history go? (operator: 'pretty sure you can get
historical OI'). /option-contract/{id}/historic is in the accessible category, SEPARATE
from the stock-history endpoints where the ~7-day 403 ceiling was observed. If its daily
bars carry open_interest with real depth, positioning gains a long OI history per contract
without the date= ceiling.

    railway run uv run python scripts/probe_oi_history.py [OCC_SYMBOL]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.services.uw_client import UWError, get  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--save"]
    save = "--save" in sys.argv
    sym = args[0] if args else "SPY260717P00710000"
    print(f"=== /option-contract/{sym}/historic ===")
    try:
        r = get(f"/option-contract/{sym}/historic").json
    except (UWError, ValueError) as e:
        print("ERR:", e)
        return 1
    if save:
        out = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bronze" \
            / "option-contract-historic" / f"{sym}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
        print("saved fixture:", out)
    rows = (r.get("chains") or r.get("data") or r) if isinstance(r, dict) else r
    if not isinstance(rows, list) or not rows:
        print("empty/odd payload:", json.dumps(r)[:300])
        return 0
    print("rows:", len(rows))
    print("keys:", ", ".join(sorted(rows[0].keys())))
    dates = sorted(x.get("date", "") for x in rows if isinstance(x, dict) and x.get("date"))
    if dates:
        print(f"span: {dates[0]} -> {dates[-1]}  ({len(dates)} daily rows)")
    print("newest row:", json.dumps(rows[0])[:300])
    print("oldest row:", json.dumps(rows[-1])[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
