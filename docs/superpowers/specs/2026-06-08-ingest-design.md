# Ingest — Design (v3, Phase 1)

**Status:** PLAN (awaiting approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** Phase 0 (contracts — `RawRecord`); `server/services/governor.py`; `server/services/storage.py`
**Starting point:** `server/pipeline/ingest.py` (scaffold exists; contract + bronze write already drafted)

## Purpose / role

Fetch one UW endpoint (governor-gated), persist the verbatim JSON into bronze with
metadata, and return a `RawRecord`. That is the entire job. No parsing; no field access;
no transformation of any kind. The boundary is strict: the only thing that ever reads
bronze is Normalize — and the only thing Normalize receives from Ingest is a `RawRecord`.

Ingest also owns the REPLAY path: when `REPLAY=1` (or the governor denies a live call
because the budget ceiling is reached), `_read_bronze` returns the latest matching
bronze row as a `RawRecord` with `from_replay=True`. The downstream code path is
**identical** — Normalize, Derive, Decide, and Present never know the difference. This
is what makes offline dev work without any stub or code-path fork.

## Contract (typed in/out)

```
ingest(endpoint: str, params: dict | None, *, ticker: str | None,
       priority: Priority) -> RawRecord
```

- **Input:** endpoint path (hyphenated UW path, e.g. `"flow-alerts"`), optional params
  dict, optional ticker label for partitioning, governor priority.
- **Output:** `RawRecord{endpoint, params, ticker, fetched_at, content_hash, payload,
  from_replay}` — the verbatim UW JSON plus metadata. `payload` is the raw dict;
  nothing in it has been touched.
- **Error contract:** raises `UWError` only when both the live call AND the bronze
  fallback fail (no bronze at all for this endpoint+params). Never returns a partial or
  silently empty record.

## Responsibilities

**Ingest owns:**
- Calling `governor.get()` before any live network call (priority scheduling, coalescing,
  budget accounting — `UW_BUDGET_SOFT_PCT` guard lives in the governor, not here).
- Writing one immutable bronze parquet part per fetch: atomic `tempfile → os.replace`,
  append-only, never mutating an existing part. Partition key = hyphenated endpoint slug.
- `content_hash` (`sha256[:16]` of `json.dumps(payload, sort_keys=True)`) — enables
  dedup checks at the bronze level without full payload comparison.
- Reading the latest matching bronze row when the live call is denied or fails
  (`_read_bronze`): query by `endpoint` + `params_json` ordered `fetched_at DESC limit 1`.
- Populating `from_replay=True` on the returned `RawRecord` when bronze was used.

**Ingest does NOT own:**
- Deciding which endpoints to fetch or in what order (caller / orchestrator).
- Parsing, validating, or field-accessing `payload` (Normalize's job).
- Deduplication logic beyond writing one part per call (compaction is a separate cron).
- TTL / cache freshness decisions — callers decide whether to call ingest at all.
- Setting `REPLAY` mode — that's the governor's / environment's concern.
- Backoff/retry on 429: that lives in `server/services/uw_client.py` (port from
  `e1d6c5e:server/uw.py` `_backoff_get`).

## Key behaviors / edge cases

- **flow-alerts is unrecoverable.** The feed returns the LAST N alerts (truncation
  artifact; `e1d6c5e:server/snapshot.py` §flow-alerts-truncation note). Unlike OI or
  GEX (backfillable by `date=`), there is no historical re-fetch for a missed session
  window. The governor must give `flow-alerts` at least `Priority.HIGH`; callers that
  drop it silently are a data-integrity risk. Ingest surfaces `from_replay=True` so
  callers can decide whether a stale bronze is actionable.
- **Hyphenated paths.** UW paths are HYPHENATED (`flow-alerts`, `spot-exposures`,
  `historical-risk-reversal-skew`). An underscore 404s silently. `_ep_key` must
  preserve hyphens for the partition name; tests in `tests/test_uw_paths.py` lint
  this at CI (port from v2; do not remove).
- **Params JSON stability.** `params_json` is `json.dumps(params, sort_keys=True)` —
  the bronze lookup key. Key ordering must be deterministic; any deviation creates a
  phantom miss.
- **REPLAY parity.** A bronze row written by a live fetch and re-read by `_read_bronze`
  must produce bit-for-bit the same `RawRecord.payload`. No transformation is applied
  on write or read.
- **Concurrent writes.** Multiple concurrent ingest calls for the same endpoint (e.g.
  per-ticker ticker loop) write independent part files. No locking needed; append-only
  semantics handle this.
- **Empty `payload` dict.** UW can return `{}` on a valid 200. Ingest writes it verbatim
  (hash of `{}` is deterministic). Normalize must detect and handle this; Ingest must
  NOT swallow it.

## Keepers to port from v2

- `_hash(payload)` → `sha256[:16]` of sorted JSON — already in scaffold; keep exactly.
- `_ep_key(endpoint)` — strip leading `/`, replace `/` with `_`; already in scaffold.
- `_read_bronze` query pattern — `params_json = ?`, `fetched_at DESC`, `limit=1`; already
  in scaffold; verify DuckDB SQL syntax vs v2's `storage.read_endpoint` wrapper.
- `uw_client.get` 429 backoff from `e1d6c5e:server/uw.py` (`_backoff_get`, `UWError`,
  exponential back-off with jitter, header-based `x-ratelimit-remaining` read).
- Budget persistence: `uw_budget.json` survives redeploys (from `e1d6c5e:server/budget.py` —
  now lives in the governor; ingest should not duplicate it).

## Acceptance criteria

- [ ] `ingest("flow-alerts", {...}, ticker="SPY", priority=Priority.HIGH)` writes one
      bronze part and returns a `RawRecord` with `from_replay=False`; `payload` equals
      the raw UW dict; `content_hash` matches `sha256[:16]` of sorted JSON.
- [ ] With `REPLAY=1` (governor denies live), the same call returns a `RawRecord` from
      the latest matching bronze row with `from_replay=True` and an identical `payload`.
- [ ] `UWError` is raised (not swallowed) when live fails AND no bronze exists.
- [ ] Two concurrent ingest calls for different tickers write two independent bronze
      parts; no part is mutated after `os.replace`.
- [ ] `tests/test_uw_paths.py` endpoint-slug lint passes (hyphens preserved, no
      underscores introduced by `_ep_key`).
- [ ] An empty `{}` payload is written and returned verbatim (not filtered out).

## Definition of done (universal)

Typed in/out (caller receives `RawRecord`; no untyped dict escapes) · provenance: `from_replay`
flag on `RawRecord` is the ingest-level provenance bit; full `Provenance` type is
constructed in Normalize from this flag + `fetched_at` · no boundary skipped (Normalize
is the only consumer of bronze) · REPLAY-reproducible: `REPLAY=1` + same bronze archive
→ identical `RawRecord` payload on every run.

## Defers to operator

- Which endpoints to fetch per ticker, and in what priority order (orchestrator spec,
  deferred).
- How many pages to paginate for flow-alerts (currently `_SESSION_FLOW_MAX_PAGES=3` in
  v2's `build_single_row`; stays in the caller / orchestrator, not ingest).
- Retention policy for bronze (how many days before compaction cron prunes old parts).
- Whether to add a `tier` field to `RawRecord` for budget accounting visibility (nice-
  to-have; governor already tracks spend).

## Open questions / flags

- Should `RawRecord` be promoted to a pydantic model in `server/models/` now that the
  scaffold has it as a `dataclass`? Contracts spec recommends promoting only when a
  non-normalize consumer appears. Currently only Normalize reads it — keep as dataclass
  until there is a concrete second consumer.
- v2's `storage.fetch_*` wrappers (read-through TTLCache → parquet → live UW) are now
  split: Ingest is the live-or-bronze fetch; the silver/gold DuckDB read layer belongs
  to storage. Ensure no v2 `storage.fetch_*` call survives in the v3 ingest stage as
  an accidental bypass of the governor.
