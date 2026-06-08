"""Probe greek-flow sign convention (v3 Phase 2, data unknown (a)).

Operator decision: pin the sign on a FRESH clean one-sided intraday session — NOT the
stale 6/5 anchor. So this script REPORTS rather than asserts a hardcoded minute: it
surfaces the dominant-minute sign, the session sum sign, and the net-prem-ticks
cross-check, for the operator to confirm against a session they know is clean.

    railway run python scripts/probe_greek_flow_sign.py SPY            # current session
    railway run python scripts/probe_greek_flow_sign.py SPY 2026-06-08 # a past session

CALIBRATION RULE (never violate): only pin the sign on a session the operator confirms
is clean/one-sided. A mixed day makes the sign invisible.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.uw_probe_targets import unwrap_rows  # noqa: E402
from server.services.uw_client import UWError, get  # noqa: E402


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _minute(row):
    return row.get("timestamp") or row.get("time") or row.get("minute")


def main() -> int:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    date = sys.argv[2] if len(sys.argv) > 2 else None

    params = {"date": date} if date else None
    try:
        gf = unwrap_rows(get(f"/stock/{ticker}/greek-flow", params))
        npt = unwrap_rows(get(f"/stock/{ticker}/net-prem-ticks", params))
    except (UWError, ValueError) as e:
        print(f"ERROR: {e}")
        return 1

    label = date or "current session"
    print(f"=== greek-flow sign probe — {ticker}  {label} ===")
    if not gf:
        print("(no greek-flow rows — market may be closed or no history for this date)")
        return 0

    vals = [(_minute(r), _f(r.get("dir_delta_flow"))) for r in gf]
    vals = [(m, v) for m, v in vals if v is not None]
    if not vals:
        print("dir_delta_flow: present? NO — field missing/non-numeric across rows")
        return 0

    session_sum = sum(v for _, v in vals)
    peak_min, peak_val = max(vals, key=lambda mv: abs(mv[1]))
    print(f"rows: {len(gf)}   series_sum(dir_delta_flow): {session_sum:,.0f} "
          f"({'positive' if session_sum > 0 else 'negative'})")
    print(f"dominant minute: {peak_min}  dir_delta_flow={peak_val:,.0f} "
          f"({'positive' if peak_val > 0 else 'negative'})")

    # cross-check net-prem-ticks net_delta sign on the same dominant minute
    nd = None
    for r in npt:
        if _minute(r) == peak_min:
            nd = _f(r.get("net_delta"))
            break
    if nd is None and npt:
        nd = _f(npt[-1].get("net_delta"))   # fallback: last cumulative tick
        print(f"net-prem-ticks net_delta (last tick): {nd}")
    elif nd is not None:
        print(f"net-prem-ticks net_delta @ {peak_min}: {nd:,.0f} "
              f"({'positive' if nd > 0 else 'negative'})")
    if nd is not None:
        agree = (nd > 0) == (peak_val > 0)
        print(f"sign cross-check: {'AGREE' if agree else 'DISAGREE'} (greek-flow vs net-prem-ticks)")

    print("\nOPERATOR: confirm this session was clean/one-sided, then read the sign:")
    print("  if the known direction was CALL-buying and series_sum is POSITIVE → positive = call-side.")
    print("  if the known direction was PUT-buying and series_sum is NEGATIVE → negative = put-side.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
