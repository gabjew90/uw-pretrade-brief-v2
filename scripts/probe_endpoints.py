"""End-to-end UW data-health probe (v3; port of e1d6c5e:scripts/probe_endpoints.py).

Hits every endpoint the pipeline depends on via the v3 `uw_client.get` (raw network —
bypasses storage/cache, so it tests real paths + current payload shapes). Reports per
endpoint: HTTP outcome, row count, whether the KEYS THE PARSERS READ are present, and
VALUE-SANITY invariants (the layer that catches sign/semantic bugs a shape check misses).

Why: UW failures degrade SILENTLY (404/empty → 'unavailable' → fallback), so a broken
pull looks identical to 'no data right now'. This makes it loud. Run with the key
injected via the Railway bridge:

    $env:RAILWAY_API_TOKEN = [Environment]::GetEnvironmentVariable("RAILWAY_API_TOKEN","User")
    railway run python scripts/probe_endpoints.py SPY

Exit code is non-zero if any endpoint ERRORed (4xx/exception) or any CRITICAL endpoint
failed a sanity invariant — so it can gate automation.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root on path

from scripts.uw_probe_targets import build_targets, resolve_expiries, unwrap_rows  # noqa: E402
from server.services.uw_client import UWError, get  # noqa: E402


def main() -> int:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    near, far = resolve_expiries(ticker, get)
    targets = build_targets(ticker, near, far)

    print(f"=== UW data-health probe — ticker={ticker}  near={near}  ~30d={far} ===")
    errors = warns = 0
    for tgt in targets:
        crit = "*" if tgt.critical else " "
        try:
            payload = get(tgt.path, tgt.params or None)
        except (UWError, ValueError) as e:
            print(f" {crit}ERROR  {tgt.label:32} {e}")
            errors += 1
            continue
        rows = unwrap_rows(payload)
        if not rows:
            tag = "EMPTY" if not tgt.critical else "EMPTY!"
            if tgt.critical:
                errors += 1
            print(f" {crit}{tag:6} {tgt.label:32} (200 but no rows)")
            continue
        missing = [k for k in tgt.required_keys if rows[0].get(k) is None]
        warn = tgt.sanity(rows) if tgt.sanity else None
        if missing:
            warn = (warn + "; " if warn else "") + f"missing keys: {missing}"
        if warn:
            warns += 1
            if tgt.critical:
                errors += 1
            print(f" {crit}WARN   {tgt.label:32} rows={len(rows):<4} {warn}")
        else:
            print(f" {crit}OK     {tgt.label:32} rows={len(rows)}")

    print(f"--- done: {errors} error(s), {warns} warning(s) "
          f"(* = Phase-3/cross-check critical) ---")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
