"""Probe flow-alerts truncation (v3 Phase 2, data unknown (c)).

Determines whether a flow-alerts pull returns the full session or only the last N
alerts, and whether `older_than` pagination yields more. v2 saw 118→394 between loads;
cap appeared to be 500.

    railway run python scripts/probe_flow_truncation.py SPY
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.uw_probe_targets import FLOW_ALERTS_MAX, unwrap_rows  # noqa: E402
from server.services.uw_client import UWError, get  # noqa: E402


def _ts(row):
    return row.get("created_at") or row.get("executed_at") or row.get("timestamp")


def main() -> int:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    path = "/option-trades/flow-alerts"
    try:
        rows = unwrap_rows(get(path, {"ticker_symbol": ticker, "limit": FLOW_ALERTS_MAX}))
    except (UWError, ValueError) as e:
        print(f"ERROR: {e}")
        return 1

    print(f"=== flow-alerts truncation probe — {ticker}  limit={FLOW_ALERTS_MAX} ===")
    print(f"rows_at_limit_500: {len(rows)}")
    if not rows:
        print("(no rows — market may be closed or no flow)")
        return 0

    stamps = sorted([t for t in (_ts(r) for r in rows) if t])
    oldest, newest = (stamps[0], stamps[-1]) if stamps else (None, None)
    print(f"oldest_created_at: {oldest}")
    print(f"newest_created_at: {newest}")
    if oldest and newest:
        try:
            span = datetime.fromisoformat(newest) - datetime.fromisoformat(oldest)
            print(f"window_hours: {span.total_seconds() / 3600:.2f}")
        except ValueError:
            print("window_hours: (unparseable timestamps)")

    older_more = "unknown"
    if oldest:
        try:
            more = unwrap_rows(get(path, {"ticker_symbol": ticker,
                                          "limit": FLOW_ALERTS_MAX, "older_than": oldest}))
            older_more = f"yes ({len(more)} more)" if more else "no"
        except (UWError, ValueError) as e:
            older_more = f"error: {e}"
    print(f"older_than_yields_more: {older_more}")
    capped = len(rows) >= FLOW_ALERTS_MAX
    print(f"resolution: {'TRUNCATED — paginate via older_than' if (capped or older_more.startswith('yes')) else 'full session in one page'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
