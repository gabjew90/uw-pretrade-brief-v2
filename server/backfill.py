"""One-shot historical OI backfill.

The 2026-05-29 429 storm blocked archive writes during the session, leaving gaps
in the per-strike OI history that Tile 2 reads. UW Basic exposes a `date=`
parameter on oi-per-strike (within the tier's lookback), so we can re-pull past
sessions — UNLIKE flow-alerts, which is recent-only and unrecoverable.

Two things make this non-trivial and are handled here:
  1. `storage.fetch_oi_strike` (the read-through) would write the row under
     *today's* partition. We bypass it: call `uw.fetch_oi_strike` directly and
     write via `storage.write_response` with `fetched_at` = the historical day,
     so the row lands in the `dt=<day>/ticker=<T>` partition `read_oi_history`
     reads. params=None (→ "{}") marks it as that day's canonical snapshot.
  2. Idempotent gap-fill: skip any (ticker, day) that already has a partition
     (live capture or prior backfill), so re-runs never clobber real data.

Rate discipline: shares `storage._uw_call_gate` (the ≤120/min semaphore) and
honors `budget.over_soft_budget()` so a backfill can't push the live loop over.
"""
from __future__ import annotations
import logging
from datetime import date, datetime, time, timedelta, timezone

from server import budget, market_hours, storage, uw

log = logging.getLogger(__name__)

# Historical rows are stamped at a fixed late-session time so they sort after any
# intraday live capture for the same day and land in that day's partition.
_ARCHIVE_HOUR_UTC = 20


def _trading_days(now: datetime, max_days: int) -> list:
    """Trading days within the last `max_days` calendar days, EXCLUDING today,
    ordered newest→oldest (yesterday first)."""
    today = now.date()
    out = []
    for delta in range(1, max_days + 1):
        d = today - timedelta(days=delta)
        if market_hours.is_trading_day(d):
            out.append(d)
    return out


def _has_partition(ticker: str, d: date) -> bool:
    """True if a parquet partition already exists for (oi_per_strike, ticker, day)."""
    tdir = (storage._data_dir() / "raw" / "endpoint=oi_per_strike"
            / f"dt={d.isoformat()}" / f"ticker={ticker}")
    return tdir.is_dir() and any(tdir.glob("part-*.parquet"))


def _usable_oi(payload) -> bool:
    rows = payload.get("data") if isinstance(payload, dict) else None
    return bool(rows)


def _fetch_oi(ticker: str, d: date):
    """One historical OI pull through the shared rate gate. Returns payload or
    None on failure/empty."""
    try:
        with storage._uw_call_gate:
            payload = uw.fetch_oi_strike(ticker, date=d.isoformat())
    except uw.UWError as e:
        log.warning("backfill fetch failed %s %s: %s", ticker, d, e)
        return None
    return payload if _usable_oi(payload) else None


def _write_history(ticker: str, d: date, payload) -> None:
    storage.write_response(
        endpoint="oi_per_strike", ticker=ticker, params=None, response=payload,
        status_code=200, latency_ms=0,
        fetched_at=datetime.combine(d, time(_ARCHIVE_HOUR_UTC, 0), tzinfo=timezone.utc),
    )


def _probe_ok(probe_ticker: str, days: list) -> bool:
    """Oldest-first probe: if the furthest target returns data the whole window
    is supported; else fall back to the nearest day to tell an unsupported tier
    apart from a date simply beyond the lookback."""
    if _fetch_oi(probe_ticker, days[-1]) is not None:
        return True
    return _fetch_oi(probe_ticker, days[0]) is not None


def probe(tickers: list[str], max_days: int = 30, now: datetime | None = None) -> dict:
    """Cheap support check (≤2 calls). Returns {"probe": "ok"|"unsupported"|
    "skipped"|"skipped_budget", ...} so the admin endpoint can answer instantly
    before committing to the full pass."""
    now = now or datetime.now(tz=timezone.utc)
    days = _trading_days(now, max_days)
    result = {"probe": "skipped",
              "probe_ticker": tickers[0] if tickers else None,
              "window_days": max_days}
    if not tickers or not days:
        return result
    if budget.over_soft_budget(now=now):
        result["probe"] = "skipped_budget"
        return result
    result["probe"] = "ok" if _probe_ok(tickers[0], days) else "unsupported"
    return result


def backfill_oi_history(tickers: list[str], max_days: int = 30,
                        now: datetime | None = None) -> dict:
    """Probe historical OI support, then gap-fill the archive for `tickers` over
    the trading days in the last `max_days`. Returns a report dict."""
    now = now or datetime.now(tz=timezone.utc)
    days = _trading_days(now, max_days)
    report = {
        "probe": "skipped",
        "probe_ticker": tickers[0] if tickers else None,
        "window_days": max_days,
        "trading_days_targeted": len(days),
        "days_with_data": 0,
        "oldest_day_with_data": None,
        "rows_written": 0,
        "skipped_existing": 0,
        "stopped_at_budget": False,
        "by_day": {},
    }
    if not tickers or not days:
        return report
    if budget.over_soft_budget(now=now):
        report["stopped_at_budget"] = True
        return report

    if not _probe_ok(tickers[0], days):
        report["probe"] = "unsupported"
        return report
    report["probe"] = "ok"

    days_with_data = set()
    for d in days:
        if budget.over_soft_budget(now=now):
            report["stopped_at_budget"] = True
            break
        written_today = 0
        for ticker in tickers:
            if _has_partition(ticker, d):
                report["skipped_existing"] += 1
                continue
            payload = _fetch_oi(ticker, d)
            if payload is None:
                continue
            _write_history(ticker, d, payload)
            report["rows_written"] += 1
            written_today += 1
        if written_today:
            report["by_day"][d.isoformat()] = written_today
            days_with_data.add(d)

    if days_with_data:
        report["days_with_data"] = len(days_with_data)
        report["oldest_day_with_data"] = min(days_with_data).isoformat()
    log.info("backfill done: %s", report)
    return report
