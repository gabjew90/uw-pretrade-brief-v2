# Basic Platform — Lazy Request-Built Dashboard — Design

**Date:** 2026-06-05
**Scope:** Replace the always-on 120s background ingestion loop with a request-driven model. UW API calls happen on **page-load** (a light build) and **ticker-click** (a full build). A persisted "last build" gives instant loads when fresh. This is code-review item **#2** ("basic platform without the fancy background ingestion / auto-refresh; calls only on page-load or click").

**Primary driver:** **simplicity** — fewer moving parts, a system that's easy to reason about and run. (Cost is a secondary, not-guaranteed benefit; see Honest trade-offs.) The 60s auto-refresh was already removed (commit 3aa5677); this removes the remaining background machinery — the loop itself.

**Not in scope:** the skew leg / Plan 3; the exit-reference (#3); any change to the tile *contents* or the regime *computation*. This is purely a change to *when and how* data is fetched and assembled.

## Why
The background loop refreshes ~15 tickers × ~6–9 endpoints every 120s during RTH — well over 100 calls/cycle, enough that the soft budget guard sheds endpoints partway through a session. It exists to (a) discover the flow-driven hot-15, (b) seed the parquet archive over time, (c) precompute the snapshot so page-load is instant, (d) compute the market regime. Investigation showed three of these no longer require an always-on loop:
- **Hot-15 discovery** is a single `flow_alerts` call — cheap to do on demand.
- **Tile 2 multi-session OI** has an existing on-demand path: `backfill.backfill_oi_history([ticker], 7)` pulls a ticker's recent sessions from UW when it's first viewed (`server/snapshot.py:208–217`). The archive is an optimization, not a hard dependency.
- **The v0.2 percentile gates are still stubbed** (`server/history.py` raises `NotImplementedError`; `gates.compute_gates(..., history=None)`), so removing the loop breaks no live feature there.

The storage *layer* (read-through cache + parquet archive) stays — it is load-bearing and still writes on every fetch. Only the background *loop* is removed.

## Architecture

### Remove the loop
- Delete `_refresh_loop()` and the lifespan task that starts it (`server/main.py`).
- Remove loop-only knobs: `_REFRESH_INTERVAL_SECONDS`, `_CLOSED_RECHECK_SECONDS`, `SNAPSHOT_PAUSED`, `MARKET_GATE_DISABLED`, and the budget-meter *shedding* branch (the per-call budget *guard* stays).
- Lifespan keeps: `budget.load_persisted()` and a disk seed of the last snapshot into RAM for instant first paint.
- `market_hours` is **kept** but no longer gates anything — it informs freshness labels only (e.g. "market closed; this is the last build").
- `REPLAY` is **kept**: with no loop it simply means the request path builds from `cached_only` archive (page-load builds the light snapshot from parquet; clicks read parquet).

### Page-load = light build, cached
New entry point `snapshot.get_or_build_snapshot(max_age_s) -> Snapshot` used by `/` and `/snapshot.json`:
1. Read the current build (RAM `_snapshot_cache["latest"]`, else last persisted `snapshots.jsonl`).
2. If its `as_of` age `< max_age_s` (default **60s**, via env `SNAPSHOT_MAX_AGE_S`) → return it (back-to-back reloads cost 0 UW calls).
3. Else call `snapshot.build_light_snapshot()`:
   - **1** `flow_alerts` call → hot-15 ranked, with **flow gate** and **direction**. The flow gate is cross-sectional rank over the single flow-alerts payload (`gates._flow_gate`). Direction uses the flow-based basis (`opening_flow` → `total_flow`); the `gamma_fallback` leg of `derive_direction` needs per-ticker GEX, which a light build doesn't fetch — that's fine, since opening flow is the primary direction signal and the gamma read fills in on click (full build). Light rows therefore carry `direction_basis ∈ {opening_flow, total_flow}` or, if flow is genuinely ambiguous, an explicit "direction on click" state — never a fabricated gamma read.
   - Compute the market regime (existing `_build_market_regime`, ~5 SPY calls).
   - Stamp freshness, persist to disk + RAM, return.

A light row sets `is_light: true` and omits the per-ticker heavy fields (`spot_data`, OI/Structural/Cost gate inputs, tile2/tile3 payloads). The OI / Structural / Cost lights render as a neutral "—" with a "click to evaluate" affordance; the **Flow** light and **direction** render normally.

### Click = full build (mostly existing)
- Frontend `selectTicker(ticker)`: if the row has `is_light`, route through the existing `lookupTicker(ticker)` path, which calls `/api/lookup/{ticker}` → `snapshot.build_single_row(t)` (full row: all 4 gates + tile inputs), replaces the light row in `ROWS`, and caches it (server `_lookup_cache` + parquet). Non-light rows (already fully built this session) proceed directly.
- Tile 3 / Tile 4 fetch on demand exactly as today (`/api/tile3`, `/api/tile4`). The click-resilience added in 3aa5677 (fallback to lookup on `reason:"not in snapshot"`) remains and composes cleanly.
- Re-clicking a ticker already built this session is instant (served from `_lookup_cache` + within-TTL parquet).

### History degrades gracefully
- The archive still writes on every `storage._through` fetch, so history accrues for tickers you actually view.
- **Tile 2 sessions:** `build_single_row` reads the archive; when a ticker has no history yet, the existing `backfill_oi_history` pulls recent sessions from UW (skipped in REPLAY).
- **Regime vol-trend:** accrues across page-loads (each build persists SPY IV/RV); when no prior value exists it shows level-only with the honest note (existing regime honest-degrade). See the market-regime-header design's cross-plan note — this satisfies it by persisting SPY IV/RV + GEX on each light build.

## Components & interfaces
- `server/snapshot.py`
  - `build_light_snapshot() -> Snapshot` — NEW. Flow-only rows + regime; makes exactly one `flow_alerts` call plus the regime's SPY calls; no per-ticker heavy endpoints.
  - `get_or_build_snapshot(max_age_s: int) -> Snapshot` — NEW. Cache-or-build front door; persists on build.
  - `build_single_row(ticker) -> Row` — EXISTING, unchanged; the full per-ticker build used on click.
  - `refresh_snapshot()` / `_refresh_loop` — REMOVED (the heavy all-rows build and the loop).
- `server/schema.py` — `Row` gains `is_light: bool = False`.
- `server/main.py` — `/` and `/snapshot.json` call `get_or_build_snapshot`; lifespan no longer starts a loop; loop knobs removed.
- `static/index.html` — `renderWatchlist` handles light rows (Flow + direction shown; other gates "—/click to evaluate"); `selectTicker` triggers a full build for `is_light` rows via the existing lookup path.

## Error handling
- `flow_alerts` failure on a light build → serve the last persisted build (stamped stale) if present, else an honest "unavailable — could not reach UW" grid (mirrors today's empty-snapshot honesty). Never overwrite a good persisted build with an empty one.
- Regime sub-fetch failures → existing honest-degrade (posture computes conservatively; never fabricates Favorable).
- A click whose full build fails → existing Tile 3/4 "unavailable" cards + lookup-error state.

## Testing (TDD)
- `build_light_snapshot`: against a `flow_alerts` golden fixture, returns hot-15 ranked with `flow` gate + `direction` set and `is_light=True`; assert **no** per-ticker heavy endpoints were called (mock/count), and exactly one `flow_alerts` call (+ regime SPY calls).
- `get_or_build_snapshot`: returns the cached build untouched when `as_of` age `< max_age_s`; rebuilds when older; persists the rebuild to disk.
- `Row.is_light` defaults False; a full `build_single_row` row is not light.
- Regression: `/` and `/snapshot.json` return a valid snapshot with **no** background task running; warming state only on a genuine cold first build.
- Frontend: a light row renders Flow + direction and a "click to evaluate" state for the other three gates; selecting a light row triggers the lookup/full-build path (extend existing lookup tests).
- Call-count bounds: one page-load build ≈ 1 (flow) + regime SPY calls; one click ≈ the existing `build_single_row` + tile3/tile4 budget. Assert upper bounds so a regression can't silently fan out.

## Honest trade-offs (must stay true in the UI copy)
- **Not guaranteed cheaper.** For an active operator opening many tickers, total calls may be similar to the soft-capped loop. The win is **predictability + simplicity**: every call traces to an action, and a whole class of machinery (loop, gating, shedding, stale-holding) is deleted.
- **Less upfront in the grid.** Only flow strength + direction until you click. The full four-gate read is one click away.
- **Latency.** First load after the freshness window ≈ 1 flow + ~5 regime calls (~1–2s); a click ≈ ~6–15 calls (~2–4s). Re-loads within the window and re-clicks within TTL are instant.
- **Sparser history.** Longitudinal data accrues only for tickers you view (plus per-click OI backfill). Acceptable for a single operator; if longitudinal coverage ever matters more, the "thin daily writer" variant (considered and deferred) can be added without undoing this.

## Migration / rollback
- Pure server+frontend change; no data migration. The parquet archive and `snapshots.jsonl` formats are unchanged (a light snapshot is a normal snapshot with `is_light` rows).
- Rollback = revert the commit(s); the loop returns. No persisted state becomes invalid either way.
