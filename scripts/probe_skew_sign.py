"""Probe: pin the risk-reversal SIGN convention + the no-expiry series question.

(a) SIGN: derive RR25 ourselves from the greeks chain (call_IV at +0.25 delta minus
    put_IV at -0.25 delta = call-put convention) and compare with the vendor
    historical-risk-reversal-skew value for the SAME expiry. If vendor ~= -derived,
    the vendor convention is put-call (as assumed) and our negation is correct.
(b) SERIES: fetch historical-risk-reversal-skew WITH and WITHOUT expiry= and compare —
    the pipeline currently fetches without expiry; which expiry is that series for?

    railway run uv run python scripts/probe_skew_sign.py SPY
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.uw_probe_targets import make_getj, resolve_expiries, unwrap_rows  # noqa: E402
from server.services.uw_client import UWError, get  # noqa: E402

getj = make_getj(get)
_DELTA_TOL = 0.10


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def derive_rr25(rows) -> float | None:
    """call_IV at delta nearest +0.25 minus put_IV at delta nearest -0.25 (call-put)."""
    best_c = best_p = None
    for r in rows:
        cd, cv = _f(r.get("call_delta")), _f(r.get("call_volatility"))
        pd, pv = _f(r.get("put_delta")), _f(r.get("put_volatility"))
        if cd is not None and cv is not None:
            d = abs(cd - 0.25)
            if d <= _DELTA_TOL and (best_c is None or d < best_c[0]):
                best_c = (d, cv)
        if pd is not None and pv is not None:
            d = abs(pd - (-0.25))
            if d <= _DELTA_TOL and (best_p is None or d < best_p[0]):
                best_p = (d, pv)
    if best_c is None or best_p is None:
        return None
    return best_c[1] - best_p[1]


def main() -> int:
    t = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    near, far = resolve_expiries(t, getj)
    print(f"=== RR sign probe {t}  ~30d expiry={far} ===")
    try:
        greeks = unwrap_rows(getj(f"/stock/{t}/greeks", {"expiry": far}))
        with_exp = unwrap_rows(getj(f"/stock/{t}/historical-risk-reversal-skew",
                                    {"expiry": far, "delta": 25}))
        no_exp = unwrap_rows(getj(f"/stock/{t}/historical-risk-reversal-skew", {"delta": 25}))
    except (UWError, ValueError) as e:
        print("ERR:", e)
        return 1

    rr_derived = derive_rr25(greeks)
    v_with = _f(with_exp[-1].get("risk_reversal")) if with_exp else None
    v_no = _f(no_exp[-1].get("risk_reversal")) if no_exp else None

    print(f"(a) derived RR25 (call-put) from greeks: {rr_derived:+.4f}" if rr_derived is not None
          else "(a) derived RR25: n/a (chain too sparse)")
    print(f"    vendor latest WITH expiry={far}:  {v_with:+.4f}" if v_with is not None else "    vendor with-expiry: n/a")
    if rr_derived is not None and v_with is not None:
        same = (rr_derived > 0) == (v_with > 0)
        print(f"    sign relation: vendor {'SAME sign as' if same else 'OPPOSITE sign to'} derived"
              f" -> vendor convention is {'call-put (negation WRONG)' if same else 'put-call (negation CORRECT)'}")
    print(f"(b) no-expiry series: {len(no_exp)} rows, latest {v_no:+.4f}" if v_no is not None
          else "(b) no-expiry series: empty")
    if with_exp and no_exp:
        dates_w = {r.get('date'): _f(r.get('risk_reversal')) for r in with_exp}
        dates_n = {r.get('date'): _f(r.get('risk_reversal')) for r in no_exp}
        shared = sorted(set(dates_w) & set(dates_n))
        if shared:
            diffs = [abs((dates_w[d] or 0) - (dates_n[d] or 0)) for d in shared]
            print(f"    overlap {len(shared)} dates, max |with - without| = {max(diffs):.4f} "
                  f"-> series {'IDENTICAL' if max(diffs) < 1e-9 else 'DIFFERENT'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
