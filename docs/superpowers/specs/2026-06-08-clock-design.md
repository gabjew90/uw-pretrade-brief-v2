# Market Clock — Design (v3, Phase 1)

**Status:** PLAN (awaiting approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** nothing (foundation; no imports from other services)
**Starting point:** `server/services/clock.py`

## Purpose / role in the pipeline

Single source of truth for time. Every pipeline stage (Ingest through Present)
and every cross-cutting service that touches a timestamp asks the clock — nothing
calls `datetime.now()` directly. Eliminating the direct-now call eliminates v2's
forming/settled confusion, UTC-vs-ET drift, weekend/holiday mis-gates, and the
"flow appears settled when OI is still forming" edge case that corrupted Tile 2
signal direction.

## Contract (typed in/out — reference server.models + the contracts spec)

```python
# Inputs  ─ injectable `now: datetime | None = None` everywhere (deterministic tests)
# Outputs ─ simple value types; no pydantic wrapping needed at this layer

Phase          (Enum: PREMARKET | OPEN | AFTERHOURS | CLOSED)
is_trading_day(d: date)   -> bool
is_half_day(d: date)      -> bool
session_date(now)         -> date   # anchor for "whose flow are we reading"
oi_settled_through(now)   -> date   # latest session whose OI has PUBLISHED
is_forming(sess, now)     -> bool   # True when sess OI not yet published
next_trading_day(d)       -> date
prev_trading_day(d)       -> date
phase(now)                -> Phase
market_is_open(now)       -> bool
```

## Responsibilities (and explicit NON-responsibilities)

**Owns:**
- Trading calendar (NYSE/Nasdaq): full-closure holidays, half-days (1 pm ET close),
  weekend detection.
- Session phase: PREMARKET (04:00–09:30 ET), OPEN, AFTERHOURS (close–20:00 ET),
  CLOSED (otherwise / holiday / weekend).
- OI settlement cadence: a session's OI publishes ~9:15 am ET the next trading
  morning. `oi_settled_through` returns the latest session whose data is safe to
  treat as settled. `is_forming` is the guard at the Normalize and Derive stages.
- Half-day close time (13:00 ET); feeds `phase` and future intraday guards.

**Does NOT own:**
- Any TTL threshold or cache expiry (those are Storage's config, deferred to operator).
- Budget decisions — clock never calls governor.
- Any UW endpoint or data fetch.

## Key behaviors / edge cases

- `now=None` → `datetime.now(tz=ET)`. Always stored as `datetime | None`, never
  a bare date, so callers can't accidentally pass a date object.
- `session_date` on a weekend/holiday returns `prev_trading_day` — the last real
  session. Flow anchors to that session, never to a non-trading day.
- `oi_settled_through` on the morning of a trading day BEFORE 09:15 ET returns the
  session before yesterday — the previous session has not yet published either.
- `is_forming(sess)` is only True for trading days; `is_forming` on a non-trading
  date is always False (weekends have no forming OI).
- Half-day close: `phase` returns OPEN until 13:00 ET on a half-day, AFTERHOURS
  13:00–20:00 ET. Calendar entries must be kept current; stale calendar silently
  mis-gates.
- All datetime arithmetic uses `zoneinfo.ZoneInfo("America/New_York")`; no
  `pytz`, no manual UTC offset.

## Keepers to port from v2 (`git show e1d6c5e:server/market_hours.py`)

| v2 item | Where it lands in v3 |
|---|---|
| `_HOLIDAYS` set (2026–2027) | Kept verbatim in `clock.py`; extend yearly |
| `is_trading_day(d)` | Already in scaffold; same logic |
| `next_trading_day` / `prev_trading_day` | Already in scaffold |
| `market_is_open()` with buffered gate (±30 min) | **NOT ported** — v3 exposes `phase()` directly; callers decide their own gate width |
| UW-CLIENT-API-ID header | Lives in `uw_client.py`, not clock |

New in v3 (not in v2): `is_half_day`, `_HALF_DAYS`, `oi_settled_through`,
`is_forming`, `Phase` enum, `session_date`.

## Acceptance criteria

- [ ] `tests/test_clock.py` passes (existing suite).
- [ ] Half-day: `phase(13:01 ET on a half-day)` → `AFTERHOURS`.
- [ ] Half-day: `phase(12:59 ET on a half-day)` → `OPEN`.
- [ ] `oi_settled_through` at 09:14 ET on 2026-06-09 (Tuesday) → 2026-06-05 (Friday).
- [ ] `oi_settled_through` at 09:16 ET on 2026-06-09 → 2026-06-08 (Monday).
- [ ] `is_forming(2026-06-08, now=09:14 ET 2026-06-09)` → True.
- [ ] `is_forming(2026-06-08, now=09:16 ET 2026-06-09)` → False.
- [ ] `session_date` on a Saturday → the Friday.
- [ ] `is_forming` on a non-trading date (weekend) → False.
- [ ] `is_trading_day` on every entry in `_HOLIDAYS` → False.
- [ ] `now=None` paths tested alongside injectable-`now` paths (no bare `datetime.now()` in impl).

## Definition of done

Typed in/out · provenance on every value (clock emits plain values; upstream callers
attach `Provenance` based on what the clock tells them) · no boundary skipped ·
REPLAY-reproducible (deterministic via injectable `now`).

## Defers to operator

- Exact `_OI_PUBLISH` time (currently 09:15 ET — confirm against live observations).
- `_PREMARKET` start, `_AFTERHOURS_END` boundaries (currently 04:00 / 20:00 ET).
- Holiday/half-day calendar past 2027.

## Open questions / flags

- Should `_HALF_DAYS` be operator-configurable (env var override) for election-day
  early closes not yet in the hardcoded set? Recommend: keep hardcoded for now,
  flag for operator review annually.
- `oi_settled_through` uses "strict next trading day at 09:15". If UW publishes
  earlier or later than 09:15, calibrate `_OI_PUBLISH` — there is no automated
  check today.
