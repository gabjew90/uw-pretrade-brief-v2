# UW Pretrade Brief v2

A personal weekly-options **decision-support** dashboard. It hydrates a single static HTML page with live Unusual Whales (UW) data + Gemini-generated insights, archives every UW response to a parquet store, and reduces the raw flow into an honest, gated read on whether a directional weekly is worth owning right now.

It never claims edge, alpha, or a backtested win rate. It surfaces what the data says, marks how fresh that data is, and is willing to tell you to stand down.

**Live demo:** the deployed Railway URL is the operator's personal instance — fork and self-host with your own keys.

---

## What it shows

A grid of "hot" tickers (plus any you pin or search), each opening into a four-tile deep-dive, under a live **market-regime header**.

- **Market-regime header** — one read before you scroll: is this a market for owning weekly premium? Computed from SPY dealer-gamma (trend vs chop), a market-wide macro-event veto (FOMC/CPI/jobs within the 1–5d hold window), buyer-framed vol (IV vs RV), a tape-flow badge, and an OPEX-week flag. Synthesized into **Favorable / Mixed / Stand down**. It is a *regime* read, never a direction call.
- **Tile 1 — Flow Alerts:** unusual options alerts (strike, premium, side, time) plotted over the 5-minute price line.
- **Tile 2 — Positioning Reality Check:** 5-session open-interest history per strike — is the flow opening new positions or closing old ones?
- **Tile 3 — Structural Setup Ladder:** dealer-gamma ladder (flip level, call/put walls) near spot; an on-demand "rich" mode adds per-expiry detail with an OI/Vol toggle.
- **Tile 4 — Contract Picker & Final Gate:** scores the weekly chain on six factors (Flow · Campaign · Room · Target · Execution · Greeks), applies hard event/IV-rank gates, and either names a contract or says no qualifying contract / stand down.

Each per-ticker view also carries a **direction** (long/short) with an explicit `direction_basis` tag, and a four-light **gate** read (Flow / OI / Structural / Cost).

---

## Architecture

FastAPI server, one static HTML page, a parquet archive, and a thin set of pure-Python decision modules.

### Data flow

```
UW REST API ──> storage._through (read-through cache) ──> parquet archive ($DATA_DIR/raw/…, append-only)
                      │                                          │
                  RAM TTLCache                            snapshots.jsonl (append-only, one per build)
                      │
   get_or_build_snapshot (request-driven, per-namespace TTL) ──> Snapshot (light rows + regime) ──> / · /snapshot.json
                                                        ▲
                          on-demand routes (/api/tile3, /api/tile4, /api/lookup) build the FULL row when a ticker is clicked
```

- **Request-driven — no background loop.** `/` and `/snapshot.json` call `snapshot.get_or_build_snapshot()`. It serves the last persisted build when fresh, else builds a **light grid** with a single `flow_alerts` call (hot-15 ranked + Flow gate + flow-derived direction; the other gates show "click to evaluate"). The regime is computed/carried on its own TTL. Two windows: `SNAPSHOT_MAX_AGE_S` (flow, default 60 s) and `REGIME_MAX_AGE_S` (default 600 s) — a reload past the flow window re-pulls flow (~1 call) but carries a still-fresh regime forward.
- **Click = full build.** Clicking a (light) ticker builds the full row on demand via `/api/lookup` → `build_single_row` (all 4 gates + tile inputs), then Tile 3-rich / Tile 4 fetch on demand. Re-clicks are cache-cheap. Every UW call traces to a page-load or a click.
- **Frozen-grid honesty.** With no loop and no auto-refresh, the grid is static between loads — staleness is shown loudly ("flow as of HH:MMz") with a one-call **"refresh flow"** button (re-pulls flow, never disturbs an open deep-dive). The deep-dive carries its own `as_of`.
- **Search any ticker:** `/api/lookup/{ticker}` builds a full dashboard row for any symbol on demand, caches it server-side, and injects it like a hot ticker. (Same path a light hot-15 row uses on click.)

### Read-through cache & archive (`server/storage.py`)

Every UW call goes through `_through()`: RAM TTLCache → parquet (within TTL) → live UW (then write parquet + cache). TTL tiers: **hot 60 s** (gamma/OI/darkpool/flow/stock-state), **medium 300 s** (IV/term-structure/tides/Tile-4 inputs), **news 900 s**, **quasi-static 24 h** (earnings/ticker-info/economic-calendar). Archive layout:

```
$DATA_DIR/
  raw/endpoint=<name>/dt=YYYY-MM-DD/ticker=<TICKER>/part-HHMM.parquet
  snapshots.jsonl        # one snapshot per cycle (boot-seed + replay source)
  sticky.json            # tracked-universe decay state
  uw_budget.json         # persisted UW call meter (survives redeploys)
```

The storage layer is **load-bearing** for the v0.2 percentile gates — it is not optional.

### Budget meter (`server/budget.py`)

UW Basic is the binding constraint (120/min, daily cap ~15 000). The meter prefers UW's authoritative response headers (`x-uw-token-req-limit`, `x-uw-daily-req-count`); falls back to `UW_DAILY_CAP`. `UW_BUDGET_SOFT_PCT` (default 90%) is a per-call guard. The count persists to `uw_budget.json` so a redeploy doesn't reset the meter to zero (a past cause of silently blowing the cap). With the background loop gone, steady-state spend is now driven by actual page-loads and clicks.

### Freshness envelope (`server/freshness.py`)

Every on-demand view is stamped with `as_of` (the **oldest** field's observation time = true build age) and `data_provenance` (**worst case** across fields: `live` / `cache` / `archive`). The frontend tints stale tiles. Provenance is collected via a contextvar that is explicitly propagated into the `ThreadPoolExecutor` pool (contextvars don't cross threads by default).

### Signal honesty — gates & direction (`server/gates.py`)

Single source of truth: gates are computed server-side and rendered as-is (no client recompute).

- **Direction** (`derive_direction`): leads from **opening** flow (`all_opening_trades` — opening bets carry the directional information), falls back to total net premium, then to a legacy gamma rule. The basis is tagged: `direction_basis ∈ {opening_flow, total_flow, gamma_fallback}` (plus `operator_override` when you flip it manually in the UI).
- **Four gates** (`compute_gates`): **Flow** (cross-sectional rank), **OI** (top-strike Δ%), **Structural** (gamma flip + wall distance — capped at yellow when `gex_sign == "POS"`, because long dealer gamma can't justify a green directional read), **Cost** (IV-rank + earnings + **macro-event-within-hold**: a market-wide FOMC/CPI/jobs event in the hold window caps Cost at yellow/red, so a row can't read all-green while the header vetoes).

### Other modules

`uw.py` (UW client, 429 backoff, forwards usage headers) · `snapshot.py` (the build pipeline + regime wiring) · `gex.py` (flip/walls math) · `tile3_detail.py` / `tile4.py` (on-demand tiles) · `market_regime.py` (pure regime synthesis) · `insights.py` (Gemini wrapper + deterministic fallback) · `universe.py` (pinned ∪ indices ∪ sticky ∪ hot-15) · `cache.py` (TTLCache primitive) · `market_hours.py` (RTH gate) · `backfill.py` (historical OI gap-fill) · `history.py` (v0.2 percentile-gate stub).

---

## Local replay / offline dev (no market, no UW key, no rate limits)

Develop and verify the whole dashboard offline against the real captured prod archive:

```bash
python scripts/pull_archive.py --token "$BACKFILL_TOKEN"   # downloads prod DATA_DIR → ./data (gitignored)
DATA_DIR=./data REPLAY=1 .venv/Scripts/python -m uvicorn server.main:app --port 8000
```

Open http://localhost:8000 and click any ticker — all tiles, including on-demand Tile 3 rich and Tile 4, render from the archive. `REPLAY=1` skips the background loop and forces the request path to `cached_only` (reads parquet, never calls UW); in cached-only mode the parquet TTL is ignored so aged data still replays. `/admin/export` (token-guarded) streams a tar.gz of `DATA_DIR` for re-pulling a fresh archive.

---

## Self-host

You need a UW API key (Basic tier or higher), a Google Gemini key (Flash-Lite is fine; insights degrade to deterministic rules without it), and any container host.

```bash
git clone <your-fork-url> uw-pretrade-brief-v2
cd uw-pretrade-brief-v2
pip install -r requirements.txt          # or: uv sync

# secrets
cp .env.example .env                      # fill UW_API_KEY (+ GEMINI_API_KEY)

uv run uvicorn server.main:app --reload   # → http://localhost:8000
# or: docker build -t uw-v2 . && docker run -p 8000:8000 -v $(pwd)/data:/data --env-file .env uw-v2
```

### Environment variables

| Var | Purpose | Default |
|-----|---------|---------|
| `UW_API_KEY` | UW auth (**required**) | — |
| `GEMINI_API_KEY` | Gemini insights (optional; falls back to rules) | — |
| `DATA_DIR` | Archive + snapshots path | `/data` |
| `REPLAY` | Offline mode — no UW calls, serve archive | off |
| `BACKFILL_TOKEN` | Unlocks `/admin/backfill` + `/admin/export` | — (disabled) |
| `TICKER_PIN_LIST` | Comma-separated tickers tracked forever | — |
| `UW_DAILY_CAP` | Daily call cap fallback (if no UW header) | `15000` |
| `UW_BUDGET_SOFT_PCT` | Soft cap (per-call budget guard) | `0.9` |
| `SNAPSHOT_MAX_AGE_S` | Flow-grid freshness window (reuse cached build under it) | `60` |
| `REGIME_MAX_AGE_S` | Regime freshness window (carried forward under it) | `600` |
| `REGIME` | Manual override for the regime label (`normal`/`risk-off`) | `normal` |

> The old `REGIME_DETAIL_TEXT` static banner string is superseded by the computed market-regime header; `REGIME` is retained only as a manual label override / fallback.

## Deploy to Railway

1. Push to GitHub. 2. New Railway project → connect repo → it autodetects the Dockerfile. 3. Add a volume named `data`, mount at `/data`. 4. Set `UW_API_KEY` (+ `GEMINI_API_KEY`, optional `TICKER_PIN_LIST`, `BACKFILL_TOKEN`). 5. Deploy — pushes to `main` auto-deploy.

## Testing

`python -m pytest -q` runs fully offline against golden fixtures. Schema-contract tests (`tests/contracts.py`) catch UW field drift at CI; live probes are opt-in (`-m live`) and skipped by default. `tests/test_html_preservation.py` guards the prototype HTML against *accidental* drift (deliberate UI changes update the test).

## License

MIT. Personal use only — don't re-serve UW data to other users (API tier constraint).
