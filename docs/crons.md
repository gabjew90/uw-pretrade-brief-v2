# Operations crons (ops-ci spec §5–7)

None of these run in the request path. Schedule on Railway cron (or run manually via the
bridge). All are idempotent.

| When (ET) | Command | What |
|---|---|---|
| 02:00 daily | `python scripts/backup_bronze.py` *(not yet written)* | tar bronze → object storage / litterbox. Bronze is the irreplaceable raw log. |
| 03:00 daily | `python scripts/compact.py --min-parts 10 --include-bronze` | merge small parquet parts per partition (content-preserving). Bronze merge is operator-approved growth control; rows are never modified. |
| 03:30 daily | `python scripts/backtest_replay.py --ticker SPY --label daily` | append today's re-derived signal row to `gold/backtest/daily/signal_history.jsonl` — the "what did the tool say" history. |

Manual probes (run before trusting UW-touching changes, via the bridge):
`probe_endpoints.py`, `probe_skew_sign.py`, `probe_oi_history.py`, `probe_oi_depth.py`,
`probe_flow_truncation.py`.

If the Railway plan tier lacks cron, run the three commands manually after each session
(`railway run uv run python scripts/...`).
