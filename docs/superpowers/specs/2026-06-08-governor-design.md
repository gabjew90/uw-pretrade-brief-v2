# Request Governor — Design (v3, Phase 1)

**Status:** PLAN (awaiting approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** `server.config` (settings), `server.services.provenance` (denial → provenance note)
**Starting point:** `server/services/governor.py`

## Purpose / role in the pipeline

Centralizes the UW budget as a priority scheduler. No pipeline stage or cross-cutting
service makes a live UW call without first passing through `governor.check()`. A denied
call returns a typed `Decision(allow=False, reason=...)` which the caller converts to
`provenance.unavailable(decision.reason)` — the denial surfaces through provenance, never
as a silent null. In REPLAY, every live call is denied unconditionally; stages read bronze.

## Contract (typed in/out — reference server.models + the contracts spec)

```python
class Priority(IntEnum):
    CRITICAL = 0   # direction-deciding: flow, OI for the chosen side
    NORMAL   = 1   # confirming context: gamma, skew, cost
    LOW      = 2   # nice-to-have: news, seasonality, sector tide

@dataclass
class Decision:
    allow:       bool
    reason:      str = ""        # maps to provenance.note on denial
    spent_today: int = 0
    cap:         int = 0

governor.check(*, priority=Priority.NORMAL) -> Decision
governor.record(n=1)                        -> None
governor.update_from_headers(headers)       -> None   # NEW: ingest UW response headers
governor.snapshot()                         -> dict   # /health diagnostic payload
```

## Responsibilities (and explicit NON-responsibilities)

**Owns:**
- **Per-minute enforcement**: sliding 60-second window; denies when `calls_1m >= 115`
  (headroom under 120/min hard limit). v2 only *observed* the window — v3 enforces it
  as a hard gate (this is an explicit improvement over v2).
- **Per-day enforcement**: prefers UW response headers (`x-uw-token-req-limit`,
  `x-uw-daily-req-count`) as authoritative; falls back to local counter + env-var cap
  (`UW_DAILY_CAP`, default 15 000 conservatively per v2 budget.py lesson).
- **Soft-cap shedding**: once `spent_today >= cap * soft_pct`, `Priority > CRITICAL`
  is denied. `soft_pct` from `UW_BUDGET_SOFT_PCT` (default 0.9).
- **Hard-cap gate**: `spent_today >= cap` denies ALL priorities.
- **REPLAY gate**: `settings.replay=True` → `Decision(False, "replay: read bronze")` for
  every call, unconditionally, before any counter check.
- **Request coalescing**: identical in-flight `(endpoint, params)` pairs share one live
  call; latecomers wait for the first response and receive the same `UWResponse`. Coalescing
  is keyed on `(endpoint, frozenset(params.items()))`.
- **Persistence**: meter state (`day_key`, `day_count`) written to `uw_budget.json` on
  the volume every `_PERSIST_EVERY` calls (atomic temp→replace), so a redeploy does not
  reset the daily counter to zero. Read at startup via `governor.load_persisted()`.
- **`update_from_headers`**: accepts a case-insensitive headers mapping; extracts
  `x-uw-daily-req-count`, `x-uw-token-req-limit`, `x-uw-req-per-minute-remaining`.
  When present, these override the local counter as the authoritative source for the
  next `check()`. Called by `uw_client.get()` after every successful response.
- **`snapshot()`**: returns `{calls_1m, calls_today, daily_cap, budget_pct,
  minute_remaining, source, day}` for the `/health` endpoint.

**Does NOT own:**
- The actual HTTP call (that is `uw_client`'s job).
- Storage reads/writes (Storage's job).
- Retry logic (uw_client's job).
- Clock queries (Governor does not call clock directly; it uses UTC wall time for the
  daily bucket reset and the 60-second window).

## Key behaviors / edge cases

- **Daily reset**: UTC midnight resets `day_count` and clears the header-sourced values;
  the next UW response re-seeds the new-day counts. The reset is detected lazily in
  `record()` and `check()` (no background thread needed).
- **Per-minute enforcement (v3 improvement)**: v2's `over_soft_budget()` only gated the
  soft cap; the 120/min limit was advisory. v3's `check()` also denies at `calls_1m >= 115`
  regardless of priority. This prevents the 429 cascade that triggered the 2026-05-29 outage.
- **Coalescing**: a second call with the same key while the first is in-flight blocks
  until the first completes, then shares its `UWResponse`. If the first raises `UWError`,
  all waiters receive the same error. Implemented with `threading.Event` per in-flight key.
- **Header authority**: if headers supply a limit lower than `UW_DAILY_CAP`, the header
  value wins (UW's cap is real; the env var is a conservative fallback).
- **Thread safety**: `_Meter` and the coalescing map are protected by `threading.Lock`.
  All mutations happen under the lock; readers get a snapshot.

## Keepers to port from v2 (`git show e1d6c5e:server/budget.py`)

| v2 item | Where it lands in v3 |
|---|---|
| `record_usage_headers(headers)` with `x-uw-*` header parsing | `governor.update_from_headers()` — same logic, same header names |
| `_uw_daily_count` / `_uw_daily_limit` / `_uw_minute_remaining` preference over local | `_Meter` fields; `check()` uses them when not None |
| `load_persisted()` / `_flush_locked()` / `_persist_path()` with atomic tmp→replace | `governor.load_persisted()` + `_persist()` — identical pattern; path via `settings` not env var |
| `_PERSIST_EVERY = 25` throttled flush | Kept as a constant; value deferred to operator |
| `_roll(now)` pruning 60s window + UTC-date-change detection | `_Meter.record()` inline — same logic |
| `_day_key` reset on UTC date change | `_Meter._roll()` |
| `over_soft_budget()` | Folded into `check()` as a priority gate |
| `reset()` (test support) | `governor.reset()` — test-only, same intent |
| `_daily_cap()` env-var fallback defaulting conservatively to 15 000 | `settings.uw_daily_cap` (default 15 000) |
| `_soft_pct()` env-var | `settings.uw_budget_soft_pct` (default 0.9) |

**Not ported**: module-level mutable globals (`_minute`, `_day_key`, …) — replaced by
the `_Meter` dataclass in the scaffold, which is cleaner for testing.

## Acceptance criteria

- [ ] `governor.check()` in REPLAY → `Decision(allow=False)` regardless of counters.
- [ ] `governor.check()` at `spent_today >= cap` → denied for all priorities.
- [ ] `governor.check()` at `spent_today >= cap * soft_pct`, `priority=NORMAL` → denied.
- [ ] `governor.check()` at `spent_today >= cap * soft_pct`, `priority=CRITICAL` → allowed.
- [ ] `governor.check()` at `calls_1m >= 115` → denied for all priorities (v3 per-minute gate).
- [ ] `update_from_headers` with `x-uw-token-req-limit=12000` overrides env-var cap in next `check()`.
- [ ] `load_persisted()` restores today's count on boot; previous-day persisted data is ignored.
- [ ] Flush writes atomically (`.tmp` → `os.replace`); a kill mid-write leaves only a `.tmp` orphan.
- [ ] Coalescing: two concurrent calls with identical `(endpoint, params)` issue one network request; both callers receive the result.
- [ ] `governor.snapshot()` returns `source="uw_headers"` when headers have been seen, `"local"` otherwise.
- [ ] `governor.reset()` clears all in-memory state without touching the persisted file.

## Definition of done

Typed in/out · provenance on every value (denials attach to `Provenance.note`) ·
no boundary skipped · REPLAY-reproducible (governor enforces REPLAY; no live call escapes).

## Defers to operator

- `UW_DAILY_CAP` default (currently 15 000; UW Basic docs example).
- `UW_BUDGET_SOFT_PCT` default (currently 0.9).
- `_PERSIST_EVERY` flush frequency.
- Per-minute headroom constant (currently 115 of 120).
- Coalescing timeout (how long a waiter blocks before giving up and returning unavailable).

## Open questions / flags

- **Per-minute enforcement strictness**: denying at 115 of 120 means a single burst of
  6 parallel CRITICAL calls right at the threshold could all be denied. Should CRITICAL
  calls bypass the per-minute gate entirely, or just raise the threshold to 119?
  Recommend: defer to operator; flag that per-minute denial for CRITICAL is a new
  behavior vs v2.
- **Coalescing scope**: should coalescing be per-governor (process-wide) or could a
  future multi-worker deploy need a shared coalescing layer? At personal-use / single
  worker, process-wide is correct.
- **7-day rolling ceiling** (mentioned in CLAUDE.md): not yet modeled in v2 or the v3
  scaffold. UW Basic may enforce this; flag for operator to verify whether it is a
  real API constraint before implementing.
