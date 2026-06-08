# UW Pretrade Brief v3 — Architecture

This is the locked structural design for v3. It exists because v2's root problem was
not any single bug — it was **fused pipeline stages**. v2's `snapshot.py` (1,248 lines)
did six jobs at once (fetch, normalize, derive, decide, generate insights, persist),
and the 3,810-line `index.html` *re-derived* part of the decision (a client-side
CALIBRATION block duplicated server gate thresholds; flip-distance and IV-curve shape
were computed twice). When fetch / derive / decide / render aren't separated by typed
contracts you get exactly v2's recurring bug families: direction logic right in one
place and wrong in another, a JS hardcode contradicting the server, storage corruption
from in-place rewrites, dead signal legs masked by honest-degrade, and clock/settlement
confusion. Those are not "fix the line" bugs — they are "the boundaries are missing"
bugs. v3 adds the boundaries.

## Framing

Single-user, personal-license, Basic-tier, end-of-day/pre-market decision tool. The
enterprise patterns worth importing are about **correctness, provenance, and iteration
speed** — not availability, horizontal scale, or streaming. One FastAPI process with
local columnar storage; the entire engineering budget goes to what makes a *signal
product trustworthy*. Kafka / microservices / k8s here would be the amateur move
dressed up as sophistication (and would violate the personal-use license).

## The 5-stage pipeline (typed contract between each stage)

### 1. Ingest → immutable raw log
Fetch each endpoint; store the response verbatim with metadata (fetch time, params,
tier, content hash). Append-only, one file per fetch, atomic temp-write→rename. No
transformation. Deletes the read-modify-write corruption class; gives replay.

### 2. Normalize → canonical typed records
Parse raw → validated pydantic models (`FlowAlert`, `OISnapshot`, `GreekFlowSeries`,
…). All field-shape validation happens here, once. The net-prem-ticks-zero,
greek-flow-sign, and flow-truncation surprises become explicit failures at this
boundary instead of silent nulls three layers down.

### 3. Derive → signals, as pure functions
direction, conviction, gamma regime, skew, cost, confirmation — each a pure function
(canonical in, signal out, no I/O), with golden-fixture and property tests. The layer
that would have caught the gamma-direction inversion and the sign-convention risk
before they shipped.

### 4. Decide → verdict, in exactly one place (server)
Takes all signals as typed inputs, applies the funnel, emits a verdict plus named
reasons. Because it consumes signals by name, you structurally cannot "compute a
signal and strand it short of the verdict" — that failure mode becomes a visible
unused input.

### 5. Present → view model → dumb frontend
The server emits a view model: per element, a surface value, a detail payload,
provenance, and a plain-English label. The client renders it and computes nothing.
That one rule kills the entire split-brain class, and novice surface/tap work becomes
a view-model property rather than render-layer logic.

## Cross-cutting services

- **Market clock / session service.** Knows trading days, holidays, half-days, the
  current session phase, and the settlement cadence per data type (flow is live
  intraday; OI settles the next business morning). Every stage asks the clock instead
  of re-deriving from `datetime.now()`. The whole forming/settled, UTC-vs-ET,
  weekend-handling, flow-as-"session" bug family is a missing clock.
- **Provenance as a type, not scattered flags.** Every value carries `source`
  (live/cache/archive/derived), `as_of`, and a `quality` tag
  (real/degraded/unavailable). `is_synthetic`, honest-degrade, and freshness collapse
  into one uniform concept that flows through and renders consistently.
- **Request governor.** Centralizes the budget and the 120/min, ~15k/day, 7-day-ceiling
  limits as a scheduler with priority — direction-critical fetches beat nice-to-have
  context — plus request coalescing and graceful degradation surfaced through
  provenance.
- **Storage = append-only parquet + DuckDB read layer.** Bronze (raw) / silver
  (canonical) / gold (signals) as parquet partitions, queried with DuckDB. Query,
  never mutate; compaction is a separate cron job. Because raw is immutable,
  re-deriving gold from bronze when the signal math changes is the backtest harness
  for free.
- **Domain-model-first, tiles-as-views.** Flow, Positioning, DealerGamma, Skew, Cost,
  Regime, Verdict are first-class entities; tiles are views over them.

## Deliberately not doing
No microservices, streaming bus, k8s, HA/replication, multi-tenant, warehouse. One
process, parquet/DuckDB, cron.

## Keepers from v2
GEX math, research-grounded signal definitions, the parquet archive concept, the
budget meter, the degrade scaffolding. v3 re-homes these inside the new boundaries; it
does not re-invent the correct parts.

## Migration note (if ever porting v2 incrementally instead of fresh)
Order of leverage was: (1) extract verdict/gate logic to one server module + delete
the frontend CALIBRATION duplication; (2) make storage append-only with a DuckDB read
layer; (3) add the market clock; (4) wrap signals as pure functions with golden tests.
v3 starts already on the far side of that migration.

## The one rule (repeated because it's load-bearing)
**The frontend computes nothing.** Server emits a typed view model; client renders it.
