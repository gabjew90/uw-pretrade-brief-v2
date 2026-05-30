# UW OI Backfill + Budget Meter — Design

**Date:** 2026-05-29
**Context:** Follow-up to the 2026-05-29 outage (commit `e7ac6ce`). UW Basic
rate-limited every refresh during the session, leaving a gap in the parquet OI
archive and no visibility into call consumption. This adds (1) a one-shot
historical OI backfill to deepen the archive and (2) a UW call meter + soft
budget guard so the 40k/day cap can no longer blindside us.

## Goals

1. Recover **OI / positioning** history into the parquet archive for the days the
   429 storm blocked, deepening Tile 2's `sessions_available`.
2. Track UW call consumption (rolling 60s + daily) and shed load *before* the
   hard 429 wall, with visibility from `/health`.

## Non-goals

- Recovering **flow** data. `flow-alerts` has no `date=` param (recent-only), so
  any missed session's flow / hot-list / Tile 1 bubbles are unrecoverable.
- Backfilling GEX / IV / max-pain — no consumer reads historical values of those,
  so writing them would archive data nothing displays. (YAGNI.)
- Per-endpoint breakdown / threshold alerting (deferred; see "Full version").

## Component 1 — Historical OI backfill

**Module:** `server/backfill.py`

`backfill_oi_history(tickers: list[str], max_days: int = 30) -> dict`

- **Probe first (1 call):** `uw.fetch_oi_strike(SPY, date=<oldest target>)`. If it
  returns no usable per-strike OI, abort and report
  `{"probe": "unsupported", ...}` — answers "does N-days-back work on this tier"
  without burning the full pass.
- **Backfill (probe OK):** for each **trading day** (skip weekends/holidays via
  `market_hours.is_trading_day`) from yesterday back to `max_days`, for each hot
  ticker, `uw.fetch_oi_strike(t, date=d)`. Report tallies days with data — that is
  the discovered real lookback.

**Partition-write contract (critical):**
- Call `uw.fetch_oi_strike` **directly**, NOT `storage.fetch_oi_strike` — the
  read-through writes the row under *today's* partition (`fetched_at=now`), which
  would not deepen history. Write via `storage.write_response` with
  `fetched_at = <historical day @ 20:00 UTC>` so the row lands in the correct
  `dt=<day>/ticker=<T>` partition where `read_oi_history` reads, with `params=None`
  (→ `params_json "{}"`) so it is treated as that day's canonical snapshot.
- **Gap-fill / idempotent:** if `dt=<day>/ticker=<T>` already holds parquet (live
  capture or prior backfill), skip. Never clobbers real data; safe to re-run.
- **Rate discipline:** acquire `storage._uw_call_gate` around each call; check the
  budget guard before each call and stop cleanly (partial report) if over.

## Component 2 — UW call meter + soft budget guard

**Module:** `server/budget.py` (thread-safe; UW calls run across pool workers)

- `record_call(now=None)` — increment; called once per HTTP attempt in `uw._get`
  (**including retries** — they consume real quota).
- `snapshot(now=None) -> {calls_1m, calls_today, daily_cap, budget_pct, day}` —
  rolling-60s count + daily count that auto-resets on UTC-midnight rollover.
- `over_soft_budget(now=None) -> bool` — `calls_today >= daily_cap * soft_pct`.
- Config: `UW_DAILY_CAP` (default 40000), `UW_BUDGET_SOFT_PCT` (default 0.9).
- `now` params injectable for deterministic tests.

**Integration:**
- `uw._get`: `budget.record_call()` per HTTP attempt (single complete choke point;
  live loop *and* backfill both pass through here, so tracking is automatic).
- `storage._through`: before the UW call, if `over_soft_budget()` and endpoint is
  not `flow_alerts` (whitelisted — core read survives longest), return
  `UWFailure(endpoint, ticker, "budget guard: daily soft cap reached")` without
  hitting UW. Combined with the resilience fix, the dashboard holds last-good data
  instead of 429-cascading.
- The semaphore stays in `storage._through` (working in prod); backfill acquires it
  explicitly. Not moved, to avoid re-touching the stabilized hot path.

## Component 3 — `market_hours.is_trading_day(d: date) -> bool`

Weekday-and-not-holiday check reusing the existing `_HOLIDAYS` set. Shared by the
gate and the backfill so there is one trading-calendar source.

## Endpoint — `POST /admin/backfill?days=N`

- **Auth:** requires `?token=` (or `X-Admin-Token` header) matching env
  `BACKFILL_TOKEN`. **If `BACKFILL_TOKEN` is unset, the endpoint is disabled (403)** —
  no abusable surface by default. Invalid/missing token → 403.
- Runs the **probe synchronously**, returns its result in the JSON immediately
  (phone-visible yes/no + sample). If probe OK, launches the full backfill as a
  **background task**, returns `202 {"probe":"ok","backfill":"started","window_days":N}`.
  Final tally → logs. Probe unsupported → `200 {"probe":"unsupported"}`, no bg work.

## `/health` additions

Add a `uw` block: `{calls_1m, calls_today, budget_pct}` from `budget.snapshot()`.

## Testing (all TDD)

- **budget:** counts increment; 60s window prunes; daily count resets on UTC-date
  change; `over_soft_budget` flips at the configured threshold.
- **_through guard:** over budget → non-whitelisted endpoint returns `UWFailure`
  without calling UW; `flow_alerts` still calls through.
- **is_trading_day:** weekday true; weekend false; holiday false.
- **backfill:** probe-fail aborts before the full pass; gap-fill skips existing
  partitions; a historical pull lands in `dt=<day>` and `read_oi_history` reads it
  back; stops at budget guard with a partial report.
- **endpoint:** 403 when `BACKFILL_TOKEN` unset; 403 on bad token; probe result
  shape on valid token (probe stubbed).
- **/health:** exposes the `uw` block.

## Rollout

- Ship behind `BACKFILL_TOKEN` unset by default (endpoint dormant). Operator sets
  the token, curls `POST /admin/backfill?days=30&token=...` once to probe + backfill,
  then can unset the token to re-seal the endpoint.
- Budget meter/guard are passive/always-on; defaults keep them out of the way unless
  consumption approaches the cap.
