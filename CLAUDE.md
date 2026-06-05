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

- **Two paths.** A background loop (every 120s in RTH; skipped under `REPLAY=1`) pulls flow-alerts → hot-15, refreshes ~6 endpoints/ticker into the archive, builds the SPY market regime, assembles the `Snapshot`, and persists `snapshots.jsonl`. `/` and `/snapshot.json` serve that cached snapshot. The heavy per-expiry/per-contract work (Tile 3 rich, Tile 4) is **on-demand** on ticker-click via `/api/tile3`, `/api/tile4`, `/api/lookup` — pre-warming all 15/cycle blew the daily cap and was reverted. Don't reintroduce loop-prewarming.
- **`server/storage.py`** — read-through cache `_through()` (RAM TTLCache → parquet → live UW). TTL tiers: hot 60s / medium 300s / news 900s / quasi-static 24h. `cached_only()` contextvar blocks live calls (REPLAY + request path). Load-bearing; don't drop it.
- **`server/budget.py`** — UW Basic is the binding constraint (120/min, ~15k/day). Prefers UW headers; soft-sheds at 90%; persists the meter to `uw_budget.json` (survives redeploys).
- **`server/freshness.py`** — stamps each view with `as_of` (oldest field) + `data_provenance` (live/cache/archive, worst-case). Contextvar is propagated into the executor pool.
- **`server/gates.py`** — single source of truth (no client recompute). `derive_direction` leads from OPENING flow → total flow → gamma (tagged `direction_basis`; `operator_override` on manual flip). Four gates: Flow / OI / Structural (capped at yellow when `gex_sign=="POS"`) / Cost (IV-rank + earnings + macro-`event_within_hold`).
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
REPLAY=1 skips the background loop and forces the request path to `cached_only`
(reads captured parquet, never calls UW); the boot-seed serves the archived
snapshot. In cached-only mode the parquet TTL is ignored (replay reads aged
data). The `/admin/export` endpoint (token-guarded by BACKFILL_TOKEN) streams a
tar.gz of DATA_DIR for re-pulling a fresh archive.

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
