"""Probe oi-per-strike live-vs-settled + lookback depth (v3 Phase 2, data unknown (d)).

Confirms what oi-per-strike returns intraday vs with `date=`, and measures the actual
lookback ceiling (v2's tier probe observed 7 trading days; account-age-dependent — see
plan §Operator flags #3). Ported from `e1d6c5e:server/backfill.py` date= pattern.

    railway run python scripts/probe_oi_depth.py SPY
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.uw_probe_targets import unwrap_rows  # noqa: E402
from server.services import clock  # noqa: E402
from server.services.uw_client import UWError, get  # noqa: E402


def _snap_trading_day(d):
    return d if clock.is_trading_day(d) else clock.prev_trading_day(d)


def _rows_for(ticker, date=None):
    params = {"date": date} if date else None
    return unwrap_rows(get(f"/stock/{ticker}/oi-per-strike", params))


def main() -> int:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    now = datetime.now(tz=timezone.utc)
    print(f"=== oi-per-strike depth probe — {ticker}  probe_time_utc={now.isoformat(timespec='minutes')} ===")

    try:
        intraday = _rows_for(ticker)
    except (UWError, ValueError) as e:
        print(f"ERROR (intraday): {e}")
        return 1
    print(f"intraday_rows (no date=): {len(intraday)}"
          + (f"  sample call_oi={intraday[0].get('call_oi')}" if intraday else ""))

    yday = clock.prev_trading_day(now.date())
    try:
        y_rows = _rows_for(ticker, yday.isoformat())
        print(f"yesterday_rows (date={yday}): {len(y_rows)}"
              + (f"  sample call_oi={y_rows[0].get('call_oi')}" if y_rows else ""))
        if intraday and y_rows:
            same = intraday[0].get("call_oi") == y_rows[0].get("call_oi")
            print(f"same_data_intraday_vs_settled: {'yes (OI settled-only intraday)' if same else 'no (intraday is forming)'}")
    except (UWError, ValueError) as e:
        print(f"WARN (yesterday): {e}")

    print("--- lookback depth (date= until empty) ---")
    last_ok = None
    first_empty = None
    for offset in (1, 7, 14, 21, 30, 45, 60):
        d = _snap_trading_day(now.date() - timedelta(days=offset))
        try:
            n = len(_rows_for(ticker, d.isoformat()))
        except (UWError, ValueError) as e:
            print(f"  date=-{offset:<3}d ({d}): ERROR {e}")
            first_empty = first_empty or offset
            continue
        flag = "OK" if n else "EMPTY"
        print(f"  date=-{offset:<3}d ({d}): {n} rows  {flag}")
        if n:
            last_ok = offset
        elif first_empty is None:
            first_empty = offset
    print(f"lookback_depth_days: last_ok≈{last_ok}  first_empty≈{first_empty}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
