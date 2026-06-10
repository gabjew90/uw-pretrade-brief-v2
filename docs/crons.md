# Nightly maintenance (ops-ci spec §5–7)

**AS-BUILT (2026-06-10): in-app scheduler, not Railway cron services.** The Railway
volume mounts to exactly ONE service, so a separate cron service could never see `/data`.
`server/services/maintenance.py` runs a daemon thread inside the web service that fires
at **03:00 ET daily**, executing sequentially (all OFFLINE — zero UW calls; this is not
the forbidden data-refresh loop):

1. `scripts/backup_bronze.py` — tar.gz + sha256 of bronze → `/data/backups/`
2. `scripts/compact.py --min-parts 10 --include-bronze` — merge small parquet parts
3. `scripts/backtest_replay.py --ticker SPY --label daily` — append today's re-derived
   signal row to `gold/backtest/daily/signal_history.jsonl`

**Observability:** the result marker is on `/health` under `maintenance` (last run time +
per-task ok/note). **Manual trigger:** `POST /admin/maintenance?token=$BACKFILL_TOKEN`.
**Disable:** env `MAINTENANCE=0`. REPLAY skips automatically.

**Caveat:** backups land on the SAME volume — safe against fat-fingers, not volume loss.
Periodically download a `/data/backups/bronze-*.tar.gz` off-box (or point a future bucket
at it; Railway buckets are S3-compatible).

Manual probes (run before trusting UW-touching changes, via the bridge):
`probe_endpoints.py`, `probe_skew_sign.py`, `probe_oi_history.py`, `probe_oi_depth.py`,
`probe_flow_truncation.py`.
