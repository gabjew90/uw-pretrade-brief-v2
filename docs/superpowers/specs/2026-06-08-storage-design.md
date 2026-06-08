# Storage — Design (v3, Phase 1)

**Status:** PLAN (awaiting approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** `server.config` (settings), `server.services.clock` (date anchoring)
**Starting point:** `server/services/storage.py`

## Purpose / role in the pipeline

Durable, append-only, auditable store for every stage's output. The three tiers make
the pipeline's stages physically visible on disk: bronze is raw UW responses (Ingest
writes), silver is canonical typed records (Normalize writes), gold is derived signals
(Derive writes). DuckDB queries the parquet lake read-only; nothing in the request
path mutates a file. Bronze immutability gives replay and a free backtest harness:
re-running Derive over bronze = re-deriving gold from source truth.

## Contract (typed in/out — reference server.models + the contracts spec)

```python
# Write side (Ingest / Normalize / Derive call these; request path never does)
write_part(tier, endpoint, table: pa.Table, *, ticker=None, dt=None) -> Path
write_rows(tier, endpoint, rows: list[dict], **kw)                   -> Path | None

# Read side (all stages query; never mutate)
read_endpoint(tier, endpoint, *, where="", params=None,
              order_by="", limit=None)                               -> list[dict]
query(sql, params=None)                                              -> list[dict]

# Partition layout (Hive-style — DuckDB prune-capable)
# <tier>/endpoint=<ep>/dt=<YYYY-MM-DD>/ticker=<T>/part-<HHMMSS>-<hex>.parquet
```

Tier names: `"bronze"` | `"silver"` | `"gold"`. Roots from `settings.bronze/silver/gold`.

## Responsibilities (and explicit NON-responsibilities)

**Owns:**
- Atomic append-only writes: temp file → `os.replace`. One immutable part per write.
  Never read-modify-write; never in-place overwrite.
- Hive-partition layout so DuckDB can prune by `dt=` and `ticker=`.
- DuckDB read layer: `read_endpoint` + `query` open a fresh `:memory:` connection per
  call (no shared connection state; safe for concurrent request handlers).
- Cold-start safety: `read_endpoint` returns `[]` when the partition doesn't exist yet.
- OI history query: `read_oi_history(ticker, since_date)` as a DuckDB query over
  bronze `oi-per-strike` — the primary consumer for Tile 2 (Positioning confirmation).
  This replaces v2's `_scan_parquet` + pyarrow filter with a SQL expression.
- Backfill-to-seed for `oi-per-strike`: bronze parts written with `dt=<past-date>`
  (matching v2's `backfill.py` date= parameter pattern) are queryable immediately via
  `read_oi_history` without any schema migration.

**Does NOT own:**
- Compaction: merging many small part files into fewer large ones is a cron job
  (`scripts/compact.py`), never in the request path.
- Cache TTLs or eviction — that's Governor + the pipeline stage's responsibility.
- Snapshot JSONL (`snapshots.jsonl`) from v2 — eliminated; the gold tier replaces it.
- View JSON blobs (`save_view` / `read_view`) from v2 — eliminated; the Present stage
  serves the ViewModel directly from gold.

## Key behaviors / edge cases

- Atomic write: `write_part` writes to `<name>.parquet.tmp` then `os.replace(tmp, final)`.
  The final file is visible to DuckDB readers only after the rename; a crash mid-write
  leaves a `.tmp` orphan (safe to delete on startup).
- Concurrent writes: each part file gets a unique name (`part-<HHMMSS>-<hex8>.parquet`);
  two concurrent writes to the same partition never collide.
- REPLAY mode: `write_part` and `write_rows` are no-ops when `settings.replay=True`
  (bronze is read-only in replay; callers must not attempt to extend it).
- `read_endpoint` with no matching files: returns `[]`, never raises — cold-start and
  REPLAY can both call it safely.
- DuckDB `hive_partitioning=true` surfaces `dt` and `ticker` as virtual columns in
  `read_endpoint` results, enabling `WHERE dt >= '2026-06-01'` without scanning
  all partitions.
- `read_oi_history` anchors `dt` via `clock.session_date(now)` — never `datetime.date.today()`.

## Keepers to port from v2 (`git show e1d6c5e:server/storage.py`)

| v2 item | Where it lands in v3 |
|---|---|
| Atomic temp→`os.replace` write pattern | `write_part` — identical; already in scaffold |
| `part-<HHMMSS>-<uuid8>.parquet` naming | Already in scaffold |
| `_record_schema()` (fetched_at, params_json, status_code, latency_ms, response) | Bronze schema for the `uw-response` endpoint in Ingest |
| `write_response(endpoint, ticker, params, response, status_code, latency_ms, fetched_at)` | Becomes `write_part("bronze", endpoint, ...)` in Ingest |
| Hive partition layout `endpoint=/dt=/ticker=` | Identical in v3; DuckDB replaces pyarrow scan |
| `_scan_parquet` / pyarrow filter chain for OI history | Replaced by `read_oi_history` DuckDB query |
| `save_view` / `read_view` JSON blobs | **Not ported** — gold tier + Present ViewModel replace these |
| `snapshots.jsonl` + tail-seed | **Not ported** — gold tier replaces; boot-seed reads latest gold |
| `sticky.json` | Operator-configurable; defer to universe spec |
| `load_persisted` (budget meter seed) | Lives in Governor — not storage |

## Acceptance criteria

- [ ] `write_part` is atomic: a crash after `pq.write_table(tmp)` but before `os.replace` leaves only a `.tmp` orphan; the partition directory contains no corrupt parquet.
- [ ] Two concurrent `write_part` calls to the same partition produce two distinct files (no collision).
- [ ] `write_rows` on an empty list → returns `None`, no file created, no error.
- [ ] `read_endpoint("bronze", "flow-alerts")` on a missing partition → `[]` (no raise).
- [ ] `read_endpoint` with `where="dt >= '2026-06-01'"` does not scan partitions outside that range (verify via DuckDB explain or row-count check).
- [ ] `read_oi_history("SPY", since=date(2026,6,1))` returns rows with `ticker="SPY"` and `dt >= "2026-06-01"`, sorted ascending by `dt`.
- [ ] `write_part` in REPLAY mode → no-op, no file written, no exception.
- [ ] `query` opens and closes a `:memory:` DuckDB connection per call (no connection leak across requests).
- [ ] Backfill-written parts (`dt=<past-date>`) appear in `read_oi_history` without migration.

## Definition of done

Typed in/out · provenance on every value (write includes `fetched_at`; read results
carry enough metadata for callers to construct `Provenance`) · no boundary skipped ·
REPLAY-reproducible (all reads serve bronze; writes are no-ops in REPLAY).

## Defers to operator

- Compression codec (currently `zstd`; operator may prefer `snappy` for read speed).
- Part-file rotation granularity (currently one per write; compaction schedule).
- `DATA_DIR` / `bronze/silver/gold` root paths (configured in `settings`).
- TTL thresholds that determine when an archive read is `degraded` vs `real`.

## Open questions / flags

- Should `write_part` accept a `Provenance` argument and embed it as columns
  (`source`, `quality`, `as_of`) in the parquet file? This would make every stored
  row self-describing without a separate metadata sidecar. Recommend: yes for silver
  and gold; bronze stores the raw response verbatim (provenance is implicit: bronze =
  archive, quality determined at read time by age).
- `read_endpoint` returns `list[dict]` (untyped). Normalize callers parse into pydantic
  models immediately. Should `read_endpoint` accept a pydantic model type and return
  `list[T]`? Recommend: keep untyped at the storage layer; parsing is Normalize's job.
