# Atomic Freshness — No Mixing Live + Last-Good Within a View

**Date:** 2026-06-02
**Scope:** Guarantee that each VIEW (snapshot row, tile3 detail, tile4 picker,
lookup) is EITHER fully built-this-pull OR fully served from the last successful
pull — never a blend of some-live + some-archived fields. Operator chose:
all-or-nothing per view, atomic fallback, KEEP per-endpoint TTL caching for
budget (a "live" view may still contain TTL-cached fields, but the view's true
freshness is stamped + shown honestly).

## CORRECTION (2026-06-02, after tracing the code)

The "live build #8 fails → #1-7 live + #8-15 archived in one tile" blend
described below does NOT happen: a failed build returns `unavailable` and the
fallback does a SEPARATE full cached_only rebuild — the partial live result is
discarded, not patched. The REAL last-good mixing is different: the cached_only
fallback reads EACH endpoint's latest archived row INDEPENDENTLY, and those rows
were captured at DIFFERENT moments (spot 16:00, greeks 15:00, earnings
yesterday) → "last-good" is a Frankenstein of per-field most-recent archives, not
one coherent moment. **Phase 1 (chosen) = persist each successful on-demand view
as a COMPLETE unit, and on failure replay that WHOLE view from ONE moment**
instead of reassembling per-endpoint rows. The as-of stamp + live-build TTL-age
honesty are deferred to a later phase.

## The problem (verified)

Today data mixes freshness three ways:
1. **Per-endpoint TTLs** — each field serves live-or-cached on its OWN TTL (spot
   60s, IV 5min, earnings 24h), so one row blends fresh + hours-old fields.
2. **One `fetched_at` per row** that doesn't reflect older cached fields → the UI
   says "30s ago" while a field is hours old.
3. **Partial fallback** — `_build_with_archive_fallback` retries the whole build
   under cached_only, but the build fetches ~15 endpoints sequentially; if #8
   429s, #1-7 may be live and #8-15 archived → half-live/half-archived in ONE
   tile. THIS is the worst mixer.

Operator wants "all live OR all last-good, no mixing." We fix #3 (the real
blend) and #2 (honesty), and KEEP #1 (TTL caching) for budget — but make a build
ATOMIC so a partial failure falls the WHOLE view back to one coherent moment.

## Design

### 1. Atomic build outcome
A view build either fully succeeds (no `UWFailure` among the endpoints that feed
visible tiles) → it's a complete "live" pull, persisted as a unit; OR it has any
such failure → discard the partial result and serve the ENTIRE last-good
archived version of that view, stale-marked. No field-level patching.

- **Snapshot rows:** already atomic-ish at snapshot level (`_next_cached_snapshot`
  holds the whole last-good snapshot when a refresh yields 0 rows). Extend: if
  any hot row has a hard failure on a tile-critical endpoint, that ROW falls back
  to its last-good (per-row, from the archived snapshots.jsonl), not a blend.
  Keep it simple: row-level atomicity — a row is all-this-cycle or all-last-good.
- **tile3 detail / tile4 / lookup (on-demand):** replace
  `_build_with_archive_fallback` (which blends) with an atomic version: run the
  build; if it returns `unavailable` OR any internal endpoint failed, serve the
  last-good COMPLETE version of that view from a per-view cache (see §3).

### 2. View freshness stamp (honesty)
Each built view records the OLDEST contributing field's `fetched_at` as its
`as_of` (not the build wall-clock). `_through` returns alongside the payload the
row's `fetched_at` from parquet (or now() on a live call); the build tracks the
min across all endpoints → `view.as_of`. The frontend shows "as of <as_of>" and
a STALE badge when `as_of` is older than a freshness threshold (e.g. >5min during
market hours). So even a happy-path view with a TTL-cached field is HONEST about
its true age — no misleading single fetched_at.

### 3. Per-view last-good store
Persist each successful on-demand view (tile3/tile4/lookup) as a COMPLETE unit
(JSON on the volume, keyed by view+ticker), so the atomic fallback serves a whole
past view, not reassembled endpoints. Reuses the snapshots.jsonl pattern. The
snapshot row's last-good already lives in snapshots.jsonl.

### 4. Detecting "any failure" in a build
Builds already collect failures (e.g. `_build_dashboard_row` builds a `_failures`
list; tile4 returns `status:"unavailable"`). Define a per-view set of
TILE-CRITICAL endpoints (the ones whose absence makes the view wrong, e.g.
spot-exposures for the gamma map, option-contracts+greeks for the picker). If any
critical endpoint failed → atomic fallback. Non-critical misses (news, earnings)
don't trigger fallback (they degrade gracefully and are TTL-cached anyway).

## Implementation

- `_through` returns/records the served row's `fetched_at` (for the as_of min).
  Add a thread-safe per-build "freshness + failure" collector (contextvar), so a
  build can ask "did any critical endpoint fail, and what's the oldest field?"
  without threading it through every function.
- `build_*` functions consult the collector → set `as_of`, decide atomic outcome.
- Replace `_build_with_archive_fallback` with `_atomic_view(build_fn, view_key)`:
  run build; on critical-failure load last-good complete view; else persist +
  return, stamped with as_of and `stale: false`.
- Frontend: show `as_of` ("as of 14:32 ET") on each on-demand tile + the row;
  STALE badge keyed to as_of age; the existing tile4/tile3 stale badges feed off
  this. Snapshot `/health` already exposes age; extend to the true as_of.

## Testing (TDD)

- contextvar freshness collector: records min fetched_at + any critical failure.
- atomic outcome: a build where one CRITICAL endpoint fails → serves last-good
  complete view (not a blend); all-success → fresh, persisted, as_of = oldest.
- non-critical miss (news) does NOT trigger fallback.
- as_of stamp = min across endpoints, not wall-clock.
- per-view last-good store: persist + reload a complete view.
- Frontend: verify offline via replay — as_of + stale badge render; no half-live.

## Out of scope

True all-live (bypassing TTL on every build) — rejected for budget (~3× calls,
re-blowout risk). Unifying all TTLs to one value — rejected; keep tiered TTLs.
Per-field age display (only the view-level oldest `as_of` is shown).
