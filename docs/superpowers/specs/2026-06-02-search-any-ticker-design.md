# Search Any Ticker (on-demand full dashboard) — Design

**Date:** 2026-06-02
**Scope:** Let the watchlist search look up ANY ticker in the UW universe — not
just the hot-15 the snapshot tracks. Pressing Enter on a non-listed symbol
builds a full dashboard row on demand (all tiles), injects it into the live
ROWS, and selects it so it behaves exactly like a hot ticker. Operator chose:
full dashboard on-demand · explicit-submit trigger (Enter, not per-keystroke).

## Why

Search currently only FILTERS the already-tracked hot-15 (visibleWatchlistRows →
`ROWS.filter(startsWith)`). The empty-state even has a placeholder admitting the
intent: "In production, search would fetch on-demand from
/api/option-trades/flow-alerts?ticker_symbol=X". This wires that up for real.

## Backend — `GET /api/lookup/{ticker}`

Build a full dashboard row for an arbitrary ticker on demand, reusing the
snapshot pipeline's `_build_dashboard_row` (the SAME row the hot-15 get):

1. Fetch that ticker's flow: `storage.fetch_flow_alerts(ticker=X)` →
   `_aggregate_flow_per_ticker` for flow_info (premium/spot/rank). A searched
   ticker may have NO flow (that's why it's not hot) — fine: flow_info defaults
   to zeros, Tile 1 renders quiet, the rest build normally.
2. `row = await _build_dashboard_row(ticker, flow_info=flow_info, loop=loop)` —
   ~15 UW calls (spot-exposures, OI, vol, IV, darkpool, earnings, info, news,
   OHLC, etc.). Plus insights.
3. Return `row.model_dump(mode="json")` (same shape as a snapshot row).

- Cached ~60s per ticker (re-search is free); the per-endpoint storage TTLs
  already dedupe most calls across a session.
- Honors the budget guard (sheds if over soft cap) and the archive-fallback
  (`_build_with_archive_fallback`): if UW is rate-limited, serve last-good from
  the captured archive, else `{status:"unavailable"}`.
- REPLAY mode: cached_only, reads the archive — so a looked-up ticker that's in
  the archive renders fully offline.
- Refactor: extract the flow-fetch + row-build so both `refresh_snapshot` and
  the lookup route call one helper (`build_single_row(ticker)`), avoiding
  duplication. `_build_dashboard_row` stays as-is.

Response: the row dict on success, or `{status:"unavailable", ticker, reason}`.

## Frontend

- **Trigger:** keep instant filtering of the hot-15 on input. On **Enter** (and
  a small "↵ look up" hint), if the typed symbol is NOT already in ROWS, call
  `/api/lookup/{TICKER}`.
- Show a "looking up TICKER…" state in the watchlist while it builds.
- On success: **inject the row into ROWS** (dedupe by ticker) and `selectTicker`
  it — it then flows through the existing renderDeepDive + the on-demand
  /api/tile3 + /api/tile4 fetches exactly like a hot ticker.
- On unavailable: show "couldn't look up TICKER (rate-limited / no data)".
- Mark a looked-up row subtly ("looked up · not in today's hot list") so it's
  clear it isn't flow-surfaced and is a point-in-time build (not auto-refreshing
  like the hot-15). Re-search to refresh.
- Replace the placeholder empty-state text with the real lookup affordance.

## Honest tradeoffs (surfaced in UI)

- A looked-up ticker is point-in-time (built when searched), not auto-refreshed.
- ~15 UW calls per lookup — on-demand only, budget-guard protected.
- No flow ⇒ Tile 1 quiet; other tiles still build.

## Testing (TDD)

- `build_single_row(ticker)` helper builds a row from stubbed UW (reuses the
  existing stub_uw fixture); returns a Row.
- `GET /api/lookup/{ticker}`: returns a row dict for a valid ticker; caches;
  budget-guard + archive-fallback path returns unavailable/last-good when live
  fails; bad/empty ticker → unavailable.
- Frontend: verify OFFLINE via replay — look up a ticker present in the captured
  archive, confirm full dashboard renders; screenshot. Update
  test_html_preservation if the search handler edit trips it.

## Out of scope

Pinning a searched ticker into the per-cycle tracked universe (operator rejected
the always-track option — keeps per-cycle budget controlled). Autocomplete /
symbol validation against a UW symbol list. Auto-refresh of looked-up tickers.
