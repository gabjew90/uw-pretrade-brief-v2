# Basic Platform — Lazy Request-Built Dashboard — Design

**Date:** 2026-06-05
**Scope:** Replace the always-on 120s background ingestion loop with a request-driven model. UW API calls happen on **page-load** (a light build) and **ticker-click** (a full build). A persisted "last build" gives instant loads when fresh. This is code-review item **#2** ("basic platform without the fancy background ingestion / auto-refresh; calls only on page-load or click").

**Primary driver:** **simplicity** — fewer moving parts, a system that's easy to reason about and run. (Cost is a secondary, not-guaranteed benefit; see Honest trade-offs.) The 60s auto-refresh was already removed (commit 3aa5677); this removes the remaining background machinery — the loop itself.

**Also in scope (folded in per review):** the **archive write-path fix** (the original storage-write finding behind #2). Removing the loop drops write *volume* but not the two write defects, and the new light-build-vs-click-full-build concurrency actually makes one of them sharper — so fix it now, while volume is low and adoption is cheap.

**Not in scope:** the skew leg / Plan 3; the exit-reference (#3); any change to the tile *contents* or the regime *computation*. This is purely a change to *when and how* data is fetched, assembled, and written.

## Why
The background loop refreshes ~15 tickers × ~6–9 endpoints every 120s during RTH — well over 100 calls/cycle, enough that the soft budget guard sheds endpoints partway through a session. It exists to (a) discover the flow-driven hot-15, (b) seed the parquet archive over time, (c) precompute the snapshot so page-load is instant, (d) compute the market regime. Investigation showed three of these no longer require an always-on loop:
- **Hot-15 discovery** is a single `flow_alerts` call — cheap to do on demand.
- **Tile 2 multi-session OI** has an existing on-demand path: `backfill.backfill_oi_history([ticker], 7)` pulls a ticker's recent sessions from UW when it's first viewed (`server/snapshot.py:208–217`). The archive is an optimization, not a hard dependency.
- **The v0.2 percentile gates are still stubbed** (`server/history.py` raises `NotImplementedError`; `gates.compute_gates(..., history=None)`), so removing the loop breaks no live feature there.

The storage *layer* (read-through cache + parquet archive) stays — it is load-bearing and still writes on every fetch (now append-only). Only the background *loop* is removed. This honors the CLAUDE.md guardrail (don't drop the storage layer); the one refinement to record there is that the *percentile-gate* foundation is NOT this view-driven archive but the deferred thin daily writer (see History trade-off).

## Architecture

### Storage model: read-through cache in front of append-only immutable logs
The whole design rests on one pattern: a **read-through cache** backed by **append-only, immutable logs** — never read-modify-write. Appends are atomic and conflict-free by construction, so a page-load build and a concurrent click build can't corrupt each other, and history/audit ("what did the dashboard say at 10:43?")/replay come for free. Two logs, the *same* shape:
- **Snapshot log (`snapshots.jsonl`) — canonical for the grid.** One immutable assembled `Snapshot` appended per build. Serves instant repaint, history, and grid replay. The "store snapshots when the user loads" requirement *is* this append. Because it's unbounded append: **seed RAM from the file's tail** (read the last record, not the whole file) on boot, and **cap/daily-rotate** it (e.g. roll to `snapshots-YYYY-MM-DD.jsonl` or keep the last N) so cold-start time and disk don't creep.
- **Raw per-endpoint archive (parquet) — kept, converted to append-only.** Today it read-modify-rewrites the hour-partition file in place (the write-path bug). It becomes one immutable part file per write. It stays because it backs **deep-dive replay** (Tile 3-rich / Tile 4 are rebuilt from raw payloads under `cached_only`) and cross-restart cache persistence — neither of which the snapshot log provides.

Both are append-only; the read-modify-write subsystem is eliminated, not patched. (Per-namespace TTLs sit in front — see "Cache by rate of change".)

### Remove the loop
- Delete `_refresh_loop()` and the lifespan task that starts it (`server/main.py`).
- Remove loop-only knobs: `_REFRESH_INTERVAL_SECONDS`, `_CLOSED_RECHECK_SECONDS`, `SNAPSHOT_PAUSED`, `MARKET_GATE_DISABLED`, and the budget-meter *shedding* branch (the per-call budget *guard* stays).
- Lifespan keeps: `budget.load_persisted()` and a **tail seed** of the last snapshot into RAM for instant first paint (read the last record of `snapshots.jsonl`, not the whole file).
- `market_hours` is **kept** but no longer gates anything — it informs freshness labels only (e.g. "market closed; this is the last build").
- `REPLAY` is **kept**: with no loop it simply means the request path builds from `cached_only` archive (page-load builds the light snapshot from parquet; clicks read parquet).

### Page-load = light build, cached
New entry point `snapshot.get_or_build_snapshot() -> Snapshot` used by `/` and `/snapshot.json`. It applies **cache-by-rate-of-change** — one TTL per cache namespace, not a single global window, so a fast-moving signal and a slow-moving one never share a TTL (and a reload never re-spends a slow signal's calls when nothing moved):
- **Flow grid** — `SNAPSHOT_MAX_AGE_S`, default **60s** (tens of seconds). Governs the hot-15 + flow gate + direction.
- **Regime / gamma** — `REGIME_MAX_AGE_S`, default **600s** (minutes). Barely moves intraday; recomputing its ~5 SPY calls on every past-60s reload is waste.
- **Reference data** (ticker info, earnings, economic calendar) — hours, via the existing quasi-static 24h tier in `storage`. No new knob; just don't fold it into the grid window.

This is the same principle the read-through cache's TTL tiers already encode; the front door simply respects per-namespace ages instead of rebuilding everything together.

Logic:
1. Read the current build (RAM `_snapshot_cache["latest"]`, else last persisted `snapshots.jsonl`).
2. If the flow grid's `as_of` age `< SNAPSHOT_MAX_AGE_S` → reuse the whole build (0 UW calls).
3. Else rebuild the flow grid (**1** `flow_alerts` call). For the regime: if the cached regime's age `< REGIME_MAX_AGE_S`, **carry it forward unchanged**; only recompute it (~5 SPY calls) when it too is stale. So the common "reload after a few minutes" path spends ~1 call, not ~6.
4. Stamp freshness, persist to disk + RAM, return.

`build_light_snapshot()` produces the flow grid:
- **1** `flow_alerts` call → hot-15 ranked, with **flow gate** and **direction**. The flow gate is cross-sectional rank over the single flow-alerts payload (`gates._flow_gate`). Direction uses the flow-based basis (`opening_flow` → `total_flow`); the `gamma_fallback` leg of `derive_direction` needs per-ticker GEX, which a light build doesn't fetch.

A light row sets `is_light: true` and omits the per-ticker heavy fields (`spot_data`, OI/Structural/Cost gate inputs, tile2/tile3 payloads). The OI / Structural / Cost lights render as a neutral "—" with a "click to evaluate" affordance.

**The light direction is rendered as explicitly provisional.** Because a light build fetches no gamma, the signal-honesty "flow vs gamma — signals disagree" warning *cannot exist yet*. A bare "AAPL → calls" in the grid would imply a confidence the light build hasn't earned — and could sprout a "but gamma disagrees" caveat one click later. So the grid direction is shown as visibly unconfirmed: e.g. **`AAPL → calls (flow-only · gamma on click)`**, dimmed/badged distinctly from a full row's confirmed direction. On click → full build, the direction either confirms or gains its disagreement flag, and the provisional styling drops. This keeps the grid honest for a scan-only user, not just honest-on-inspection of the `is_light`/`direction_basis` tags.

### Click = full build (mostly existing)
- Frontend `selectTicker(ticker)`: if the row has `is_light`, route through the existing `lookupTicker(ticker)` path, which calls `/api/lookup/{ticker}` → `snapshot.build_single_row(t)` (full row: all 4 gates + tile inputs), replaces the light row in `ROWS`, and caches it (server `_lookup_cache` + parquet). Non-light rows (already fully built this session) proceed directly.
- Tile 3 / Tile 4 fetch on demand exactly as today (`/api/tile3`, `/api/tile4`). The click-resilience added in 3aa5677 (fallback to lookup on `reason:"not in snapshot"`) remains and composes cleanly.
- Re-clicking a ticker already built this session is instant (served from `_lookup_cache` + within-TTL parquet).

### Loud staleness + a one-call "refresh grid" button
With both the loop and the auto-refresh gone, the grid is **frozen between manual reloads** — a user who loads at 10:00 and doesn't reload is reading 10:00's hot-15 and flow gate at 11:00. This is a real behavioral cost, not mere "latency," and the design owns it with two mechanisms:
- **Staleness is loud, not subtle.** Past the flow window, the header shows a prominent stale state (the existing `.stale` tint plus an explicit banner like **"Flow as of 10:02 — 58m old · refresh"**), not just a quiet "58m ago." The freshness envelope already computes the age; this elevates it visually so a scan-only user can't mistake stale flow for live.
- **Manual "refresh grid" button** (1 call). Re-pulls flow — equivalent to `get_or_build_snapshot` with the flow window forced to 0 — and re-renders **only the watchlist grid + header**, never the open deep-dive (honoring the operator's standing "don't refresh under my cursor" rule; this is user-initiated, the thing they objected to was *automatic* refresh). Regime follows its own `REGIME_MAX_AGE_S` (the button doesn't force-spend SPY calls unless the regime is also stale). This restores cheap intraday currency without reintroducing background polling.
- **The deep-dive carries its own "as of."** After a grid refresh, an open deep-dive (a prior full build) can be *older* than its just-refreshed grid row — the same ticker briefly reads two ages. That's honest and intended; don't paper over it. Extend the independent-aging precedent (flow vs regime) to the deep-dive: stamp and show the deep-dive's own `as_of` (the freshness envelope already stamps per-view), so the two ages are each labeled rather than one silently masquerading as the other. A subtle "refresh this ticker" affordance on the deep-dive is optional, not required.

### Archive write-path: apply the append-only principle (atomic, race-safe)
This is the raw archive becoming append-only — the second half of the storage-model pattern above. The current writer (`storage._append_row`) read-modify-rewrites the entire hour-partition file in place: `existing = pq.ParquetFile(path).read(); concat; pq.write_table(path)`. Three defects: (1) **non-atomic** — a crash mid-write corrupts the live file; (2) **O(n²) within the hour** — every append rewrites all prior rows; (3) **lost-update race** — two concurrent builds reading+rewriting the same `endpoint/ticker/hour` file clobber each other. Removing the loop slashes write volume (so (2) mostly evaporates), and the new **light-build-vs-click-full-build** concurrency makes (3) more likely, so convert the write path to immutable appends:
- **One part file per write, atomically.** Write each record to `part-HHMMSS-<short>.parquet` via a temp file + `os.replace` (the codebase already uses this `tmp → os.replace` idiom elsewhere in `storage.py`). No read-modify-write; each write is a small, independent, atomically-renamed file.
- **Readers already glob.** `_read_latest_from_parquet` and `read_oi_history` already do `sorted(d.glob("part-*.parquet"), reverse=True)` and pick the freshest `fetched_at`, so many small part files are read-compatible with no reader change beyond confirming newest-first ordering holds for the `HHMMSS` names (it does, lexicographically within a day).
- **Deferred retention/prune — and it's a *read*-path cost, not just disk.** Compaction is YAGNI for write volume, but the read path globs + sorts *every* part file in a partition on each read. Viewing one ticker repeatedly over weeks grows its part-file count, so per-read cost creeps up even though writes stay low. Deferred, but flagged: a simple "keep last N part files per partition" prune (on write, or a tiny startup sweep) will eventually be worth adding — driven by read latency, not disk.

### History degrades gracefully
- The archive still writes on every `storage._through` fetch, so history accrues for tickers you actually view.
- **Tile 2 sessions:** `build_single_row` reads the archive via `read_oi_history`, which collapses a partition into **one OI bar per settled session, oldest→newest**. ⚠ **Correctness dependency of the append-only change:** today a partition is one file/hour; after the change it's many intraday part files. `read_oi_history`'s dedupe-to-one-per-day logic must be confirmed (and fixed if needed) to aggregate *across* part files — group rows by session date, take the settled/last value per day — rather than assuming a single file per partition. Getting this wrong silently produces wrong session bars. When a ticker has no history yet, the existing `backfill_oi_history` pulls recent sessions from UW (skipped in REPLAY).
- **Regime vol-trend:** accrues each time the regime is rebuilt (on its `REGIME_MAX_AGE_S` cadence, every build persists SPY IV/RV + GEX); when no prior value exists it shows level-only with the honest note (existing regime honest-degrade). This satisfies the market-regime-header design's cross-plan note (SPY IV/RV + GEX must keep being archived) — at a ~10-min cadence driven by real loads rather than a fixed loop.

## Components & interfaces
- `server/snapshot.py`
  - `build_light_snapshot() -> Snapshot` — NEW. Flow-only rows; one `flow_alerts` call; no per-ticker heavy endpoints. Regime is attached by the front door (below), not recomputed here, so it can be carried forward on its own TTL.
  - `get_or_build_snapshot(*, force_flow=False) -> Snapshot` — NEW. Cache-or-build front door honoring the two windows (`SNAPSHOT_MAX_AGE_S` for the flow grid, `REGIME_MAX_AGE_S` for the regime); carries a still-fresh regime forward onto a rebuilt grid; persists on build. `force_flow=True` backs the manual refresh (flow window → 0).
  - `build_single_row(ticker) -> Row` — EXISTING, unchanged; the full per-ticker build used on click.
  - `refresh_snapshot()` / `_refresh_loop` — REMOVED (the heavy all-rows build and the loop).
- `server/storage.py` — `_append_row` rewritten: one atomically-renamed part file per write (`part-HHMMSS-<short>.parquet` via temp + `os.replace`), no read-modify-write. `_partition_path` (or a new helper) yields the unique per-write filename. Readers unchanged (already glob `part-*.parquet`).
- `server/schema.py` — `Row` gains `is_light: bool = False`. `Snapshot`/`Regime` already carry `as_of`/freshness used to age the regime independently.
- `server/main.py` — `/` and `/snapshot.json` call `get_or_build_snapshot()`; a new tiny route (e.g. `GET /snapshot.json?refresh=1`, or `POST /api/refresh-grid`) calls it with `force_flow=True`; lifespan no longer starts a loop; loop knobs removed.
- `static/index.html` —
  - `renderWatchlist` handles light rows: Flow light + **provisional** direction (`(flow-only · gamma on click)`, dimmed/badged), other three gates "—/click to evaluate".
  - `selectTicker` triggers a full build for `is_light` rows via the existing lookup path (drops the provisional styling once the full row lands).
  - Loud staleness in the header past `SNAPSHOT_MAX_AGE_S`, plus a **"refresh grid"** button that hits the refresh route and re-renders only the grid + header (never the open deep-dive).
  - The deep-dive shows its **own** `as_of` (from the per-view freshness stamp), so a deep-dive older than its just-refreshed grid row reads as two clearly-labeled ages, not one masquerading as the other.

## Error handling
- `flow_alerts` failure on a light build → serve the last persisted build (stamped stale) if present, else an honest "unavailable — could not reach UW" grid (mirrors today's empty-snapshot honesty). Never overwrite a good persisted build with an empty one.
- Regime sub-fetch failures → existing honest-degrade (posture computes conservatively; never fabricates Favorable).
- A click whose full build fails → existing Tile 3/4 "unavailable" cards + lookup-error state.

## Testing (TDD)
- `build_light_snapshot`: against a `flow_alerts` golden fixture, returns hot-15 ranked with `flow` gate + `direction` set and `is_light=True`; assert **no** per-ticker heavy endpoints were called (mock/count), and exactly one `flow_alerts` call.
- `get_or_build_snapshot` — two-window behavior: (a) flow fresh → whole build reused, 0 calls; (b) flow stale + regime fresh → rebuilds grid (1 flow call), **carries the cached regime forward** (0 SPY calls), asserts the regime object is identical; (c) both stale → grid + regime rebuilt; (d) `force_flow=True` rebuilds the grid regardless of flow age; (e) every rebuild persists to disk.
- `Row.is_light` defaults False; a full `build_single_row` row is not light.
- **Provisional direction:** a light row's direction carries `is_light` + a flow-only basis and the frontend renders the provisional badge; a full row does not. (Render-level assertion in the html/JS test where practical, plus a schema/shape assertion.)
- **Archive write-path:** two concurrent `_append_row` calls for the same `endpoint/ticker/hour` both persist (no lost update) — each produces its own part file and a subsequent read sees both rows; a write is atomic (no partial/corrupt file is ever the live path — temp-then-replace). Reader returns the freshest `fetched_at` across multiple part files.
- **Tile 2 multi-session aggregation across fragmented part files (correctness-critical):** seed a partition with **many small part files spanning several settled sessions** (multiple intraday writes per day across several days), then assert `read_oi_history(ticker, 5)` returns exactly one OI bar per settled session, oldest→newest, with the correct per-session value — NOT one bar per part file, and not only the freshest value. This is the regression guard for the single-file→many-files shape change; it must fail against a reader that assumed one file per partition.
- **Refresh route:** the refresh endpoint forces a flow rebuild (1 call) and returns the new grid; does not force a regime recompute when regime is fresh.
- Regression: `/` and `/snapshot.json` return a valid snapshot with **no** background task running; warming state only on a genuine cold first build.
- Frontend: selecting a light row triggers the lookup/full-build path and drops the provisional styling (extend existing lookup tests); the refresh button re-renders the grid + header but not the open deep-dive.
- Call-count bounds: page-load after flow-window only (regime still fresh) ≈ **1** call; cold/both-stale build ≈ 1 + regime SPY calls; one click ≈ the existing `build_single_row` + tile3/tile4 budget. Assert upper bounds so a regression can't silently fan out.

## Honest trade-offs (must stay true in the UI copy)
- **Not guaranteed cheaper.** For an active operator opening many tickers, total calls may be similar to the soft-capped loop. The win is **predictability + simplicity**: every call traces to an action, and a whole class of machinery (loop, gating, shedding, stale-holding) is deleted.
- **Less upfront in the grid.** Only flow strength + a *provisional* direction until you click. The full four-gate read — and any flow-vs-gamma disagreement flag — is one click away. The provisional styling makes that explicit so the grid never overstates confidence.
- **Frozen between reloads — but loud about it.** No loop and no auto-refresh means the grid is static until you reload or hit "refresh grid." Mitigated, not ignored: staleness is shown prominently past the flow window, and the one-call refresh button re-pulls flow without a full reload or disturbing the open deep-dive.
- **Latency.** A reload after the flow window but within the regime window ≈ **1** call (~0.5–1s); a cold/both-stale build ≈ 1 flow + ~5 regime calls (~1–2s); a click ≈ ~6–15 calls (~2–4s). Reloads within the flow window and re-clicks within TTL are instant.
- **Biased history, not just sparser.** Longitudinal data accrues only for the tickers you viewed *on the days you happened to look* — that's a selection bias, not just thinner coverage. It's fine for the current live features (Tile 2 fills gaps via per-click OI backfill; regime vol-trend honest-degrades). **Hard ordering constraint:** if the v0.2 percentile gates are ever un-stubbed, they must NOT be built on this view-driven archive — the "thin daily writer" (deferred here, but the door is kept open) has to be built **first**. The door must be walked through *before* percentiles, not bolted on after. Make this explicit in the percentile-gates plan when it happens.

## Migration / rollback
- Pure server+frontend change; no data migration. The parquet archive and `snapshots.jsonl` formats are unchanged (a light snapshot is a normal snapshot with `is_light` rows).
- Rollback = revert the commit(s); the loop returns. No persisted state becomes invalid either way.
