# CLAUDE.md

Project: **UW Pretrade Brief v2** — FastAPI port of the v1 prototype with persistent archive.

## Tech stack (locked)

- Python 3.11
- FastAPI + uvicorn
- requests (UW client kept sync; wrapped in ThreadPoolExecutor for parallelism)
- pyarrow (parquet archive)
- google-genai (Gemini Flash-Lite)
- pydantic v2
- pytest + responses + pytest-mock + pytest-asyncio

Do **not** suggest Streamlit (this is the v2 specifically pivoting away). Do not suggest WebSockets, Redis, or external DBs without explicit override.

## Architecture map (current — see README.md for the full version)

FastAPI server + one static page (`static/index.html`) + parquet archive + pure decision modules.

- **Request-driven (NO background loop).** `/` and `/snapshot.json` call `snapshot.get_or_build_snapshot()`: serve the last persisted build when fresh, else build a **light grid** (one `flow_alerts` call → hot-15 + Flow gate + flow direction; other gates show "click to evaluate"). Per-namespace TTL: `SNAPSHOT_MAX_AGE_S` (flow, 60s) vs `REGIME_MAX_AGE_S` (regime, 600s, carried forward). Clicking a ticker builds the FULL row on demand via `/api/lookup`→`build_single_row`, then Tile 3-rich/Tile 4 via `/api/tile3`,`/api/tile4`. Every UW call traces to a load or click. `refresh_snapshot` (full all-rows build) is retained/tested but no longer loop-driven; **don't reintroduce a background loop or loop-prewarming** (it blew the daily cap, 2026-06-01).
- **`server/storage.py`** — read-through cache `_through()` (RAM TTLCache → parquet → live UW). **Append-only writes**: one immutable `part-HHMMSS-<hex>.parquet` per write (atomic temp→`os.replace`); readers glob+dedupe. `snapshots.jsonl` is append-only too (tail-seeded, line-capped). TTL tiers: hot 60s / medium 300s / news 900s / quasi-static 24h. `cached_only()` contextvar blocks live calls (REPLAY + request path). Load-bearing; don't drop it.
- **`server/budget.py`** — UW Basic is the binding constraint (120/min, ~15k/day). Prefers UW headers; `UW_BUDGET_SOFT_PCT` is a per-call guard; persists the meter to `uw_budget.json` (survives redeploys).
- **`server/freshness.py`** — stamps each view with `as_of` (oldest field) + `data_provenance` (live/cache/archive, worst-case). Contextvar is propagated into the executor pool.
- **`server/gates.py`** — single source of truth (no client recompute). `derive_direction` leads from OPENING flow (`volume_oi_ratio>1`) → total flow → gamma (tagged `direction_basis`; `operator_override` on manual flip). Four gates: Flow / OI / Structural (capped at yellow when `gex_sign=="POS"`) / Cost (IV-rank + earnings + macro-`event_within_hold`).
- **`server/verdict.py`** (Plan 3) — pure deep-dive `row.verdict`: collapses Flow+OI into **Positioning** (green only on opening_flow; total_flow caps yellow; archive-decoupled; unwinding caps), **Structural**, and **Skew** (25Δ risk-reversal — PRIMARY: UW vendor `historical-risk-reversal-skew` [HYPHENS — underscore 404s], sign-corrected via `extract_vendor_rr` since vendor RR = put−call; FALLBACK: `derive_rr25` from `greeks`. Asymmetric oppose-veto, agree subordinate, never a peer green), Cost demoted to a guard, `signal_conflict` + an `action` headline. Computed in `build_single_row` at a ~30d `_skew_expiry`; **deep-dive only** (None on light grid rows). Rendered by `renderVerdictPanel`.
- **`server/market_regime.py`** — pure `compute_market_regime(...)`: SPY gamma headline (trend/chop), macro-event veto (hold window), buyer-framed vol, tide badge, OPEX → **Favorable/Mixed/Stand down**. A *regime* read, NEVER a direction call (enforced in tests). Wired in `snapshot._build_market_regime`; its `event_within_hold` also feeds every row's Cost gate.
- **Other:** `uw.py` (client, 429 backoff) · `snapshot.py` (build pipeline) · `gex.py` (flip/walls) · `tile3_detail.py`/`tile4.py` (on-demand tiles) · `insights.py` (Gemini + deterministic fallback) · `universe.py` (pinned ∪ indices ∪ sticky ∪ hot-15) · `market_hours.py` · `backfill.py` · `history.py` (v0.2 percentile stub).
- **Tiles:** T1 Flow Alerts · T2 Positioning Reality Check (OI history) · T3 Structural Setup Ladder (gamma flip/walls; on-demand rich mode) · T4 Contract Picker & Final Gate (6-factor score + hard gates). Header = computed market regime.
- **Dormant-by-design (don't "clean up"):** ingest-only endpoints + cross-ticker fetches are commented out in `snapshot.py` (no consuming tile yet); `is_synthetic=True` is a render-shape flag, NOT a provenance flag; the per-call concurrency semaphore is concurrency=1, not a rate limiter. All documented inline with rationale.

Specs/plans live under `docs/superpowers/{specs,plans}/`; the most recent capture the current intent (signal-honesty, market-regime-header).

## Behavior

- Personal use only. The deployed Railway URL is the operator's; others fork + self-host with their own keys.
- Never claim alpha, edge, or backtested win rates.
- Frequent commits, conventional-commit style.
- Phone-only operator: paste file contents or diffs into chat after edits; upload binaries to litterbox.

## Local replay / offline dev (NO market, NO UW key, NO rate limits)

Develop and verify the WHOLE dashboard offline against the real captured prod
archive — don't wait for market hours or burn UW budget:

```
python scripts/pull_archive.py --token "$BACKFILL_TOKEN"   # downloads prod DATA_DIR → ./data (gitignored)
DATA_DIR=./data REPLAY=1 .venv/Scripts/python -m uvicorn server.main:app --port 8000
```

Then open http://localhost:8000 and click any ticker — all tiles, including the
on-demand Tile 3 gamma map and Tile 4 contract picker, render from the archive.
REPLAY=1 forces the request-driven build (and the on-demand tile routes) to
`cached_only` (reads captured parquet, never calls UW); the boot-seed serves the
last archived snapshot for instant first paint. In cached-only mode the parquet
TTL is ignored (replay reads aged data). NOTE: `_read_latest_from_parquet` scans
today+yesterday partitions, so replaying an archive more than ~1 day older than
the system clock falls back to the seeded snapshot — pull a fresh archive (or it
degrades gracefully to last-good). The `/admin/export` endpoint (token-guarded by
BACKFILL_TOKEN) streams a tar.gz of DATA_DIR for re-pulling a fresh archive.

## Behavior — guardrails

- The `frontend-design` plugin is **allowed** for frontend work. The earlier rule
  restricting `static/index.html` to the 6 marker-bracketed V2-EDIT zones is
  **relaxed**: deliberate edits to the prototype HTML/CSS/JS (including the tile
  render functions) are permitted when they serve a real improvement. When a
  sanctioned change trips `tests/test_html_preservation.py`, update that test to
  match the new intent rather than contorting the code to keep the old diff —
  but keep the test meaningful (it still guards against *accidental* drift).
- Don't suggest dropping the storage layer "for simplicity" — it's load-bearing for the v0.2 percentile gates.
- Don't suggest hosting on Streamlit Cloud.
