# Per-Source Freshness Envelope (Atomic-Freshness Phase 2) — Design

**Date:** 2026-06-02
**Scope:** Make every view's TRUE freshness observable. Today a view has one
`fetched_at` (build wall-clock) that hides the fact that individual fields may be
TTL-cached at different ages (a 4-min IV next to a live spot). This stamps each
view with `as_of` = the OLDEST contributing field's real observation time +
`data_provenance` = worst-case (live/cache/archive), and surfaces it per-tile so
mixing is HONEST/visible. Operator chose: per-view "as of" + stale tint.

Builds on Phase 1 (atomic last-good fallback, e9df6fa). KEEPS per-endpoint TTL
caching for budget — this is about HONESTY, not eliminating the caching.

## Mechanism — a freshness contextvar collector (mirrors budget/cached_only)

`server/freshness.py`:
- `_COLLECTOR: ContextVar[list | None]`.
- `@contextmanager collect()`: sets a fresh list, yields a handle; on exit the
  handle exposes `summary()`.
- `record(endpoint, observed_at, provenance)`: appends to the active collector
  (no-op if none active — so non-build calls are unaffected).
- `summary()` → `{as_of: min(observed_at) or None, provenance: worst-severity,
  n_live, n_cache, n_archive, oldest_endpoint}`. Severity order: archive > cache
  > live (worst wins).

## storage._through reports freshness

- `_read_latest_from_parquet` returns `(payload, fetched_at)` instead of just
  payload (the parquet row already carries `fetched_at`). [internal; callers
  updated]
- In `_through`, on each served read call `freshness.record(...)`:
  - RAM cache hit → provenance "cache", observed_at = the entry's stored
    observed_at (extend TTLCache.set/get to carry it, or store (value,
    observed_at) — see below).
  - parquet hit within TTL → "cache", observed_at = row fetched_at.
  - parquet hit in cached_only/replay (aged) → "archive", observed_at = row
    fetched_at.
  - live UW call → "live", observed_at = now.
- TTLCache: store observed_at alongside value so a RAM hit reports the ORIGINAL
  observation time, not the cache-set time. `set(key, value, ttl, observed_at)`;
  `get` returns (value, observed_at). Back-comat: observed_at optional → now.

## Builds stamp the view

`build_tile4`, `build_tile3_detail`, `build_single_row`, and the snapshot row
build wrap their fetches in `freshness.collect()` and attach to the result:
- `as_of`: ISO of the oldest contributing field (or None if no fields).
- `data_provenance`: "live" | "cache" | "archive".
Snapshot rows: stamp per-row `as_of`; the Snapshot keeps its build `fetched_at`
but rows gain honest `as_of`. (Row schema gains `as_of: str|None`,
`data_provenance: str`.)

The atomic last-good replay (Phase 1) already carries the persisted view's own
as_of (it was stamped when built) — so a stale-served view shows its true
original moment, not now.

## Frontend

Each tile (and the deep-dive header) shows "as of HH:MM ET" from the view's
`as_of`, with a subtle stale tint/badge when `as_of` age exceeds a per-view
freshness target (reuse the existing stale badge styling). Snapshot grid: the
header's existing age line keys off the oldest row as_of. No per-field breakdown
(deferred) — just the per-view oldest, which is the honesty fix.

## Testing (TDD)

- freshness.collect/record/summary: min observed_at; worst provenance; counts;
  no-op outside a collector.
- _through records the right provenance + observed_at for each path (live /
  RAM-cache / parquet-within-ttl / archive). TTLCache carries observed_at.
- build_* stamps as_of = oldest field + provenance; a build mixing a live spot +
  an aged cached field reports the AGED time as as_of and provenance="cache".
- atomic replay carries the persisted as_of (not now).
- Frontend: verify offline via replay — "as of" + stale tint render per tile;
  screenshot.

## Out of scope

Per-field age breakdown UI (only the per-view oldest is shown). Changing TTLs.
Eliminating cache mixing (kept for budget — this makes it HONEST).
