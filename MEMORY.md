# MEMORY.md

## Decisions

- **2026-05-27 — Stack pivot from Streamlit to FastAPI.** Per docs/superpowers/specs/2026-05-27-v2-production-stack-design.md. Rationale: prototype is a polished single-page HTML/SVG dashboard; Streamlit's component model is the wrong fit.

- **2026-05-27 — Storage layer with raw UW response archive.** Per spec §3a. Decision: store ALL raw UW responses to parquet partitioned by endpoint/date/ticker. Snapshots also persisted to /data/snapshots.jsonl. Volume cost ~$1.25-$2/mo on Railway. Decoupling dashboard from analytics from day 1.

- **2026-05-27 — Tracked universe = pinned ∪ indices ∪ sticky ∪ hot_15.** Per spec §3b. Fixes the churning-watchlist selection bias. Per-ticker TTL: 60s hot, 5min sleeper, 6h quasi-static (earnings).

- **2026-05-27 — Gate evolution path documented but not implemented.** Per spec §8b. Gates 1 and 2 will swap from cross-sectional/absolute to percentile-based once 30+ days of archive accumulate for a ticker. Forward-compatible signature `compute_gates(row, history=None)` in place from day 1; `server/history.py` stubbed.
