# Tile 3 — Phase 2 (Gamma Map: toggles, expiries, drift, 2×) — Design

**Date:** 2026-05-30
**Scope:** Phase 2 of the Tile 3 build spec (`tile3_build_spec.md`). Adds the
OI/Volume toggle, expiry filter, charm/vanna drift panel, call/put-separated
diverging ladder, and 2× footprint — fed by a new **hybrid on-demand route** so
the heavy per-expiry endpoints never hit the per-cycle snapshot budget. Also
fixes the ladder y-axis scale bug. Phase 1 (real single-expiry net-γ ladder in
the snapshot) stays as the at-a-glance + instant-fallback layer.

## Architecture — hybrid on-demand

- **Snapshot keeps lightweight Tile 3** (current `row.tile3` real ladder, single
  expiry, OI) for the 15-ticker view and as the instant render while the rich
  data loads.
- **New `GET /api/tile3/{ticker}`** returns the rich per-expiry payload. The
  frontend fetches it when a ticker is selected (`?t=` / click) and upgrades
  Tile 3 in place. Toggles re-render client-side off the one payload — no
  refetch per tap.
- Heavy calls (`expiry-strike`, `greek-exposure/expiry`) fire ~2–3 per
  ticker-select, cached ~60s, never per snapshot cycle. Honors the budget guard.

## Backend — `server/tile3_detail.py`

`build_tile3_detail(ticker, flow_spot, direction) -> dict` returns:

```json
{ "ticker": "...", "spot": 0.0, "direction": "calls",
  "default_expiry": "YYYY-MM-DD",
  "drift_available": true,
  "views": {
    "<expiry>": {
      "label": "Dec 22", "tag": "next Fri", "dte": 5,
      "regime": "short|long",
      "flip": 0.0, "flip_status": "ok|no_flip",
      "call_wall": 0.0, "put_wall": 0.0, "max_pain": 0.0,
      "charm": {"dir": "up|down", "strength": 0.0},
      "vanna": {"dir": "up_if_iv_falls|...", "strength": 0.0},
      "oi":  [ {"strike": 0.0, "call_gamma": 0.0, "put_gamma": 0.0, "net": 0.0} ],
      "vol": [ {"strike": 0.0, "call_gamma": 0.0, "put_gamma": 0.0, "net": 0.0} ]
    }
  } }
```

- **Expiries:** pull the available expiries from `option-contracts` / chain (or a
  dedicated expiry list), keep the front ~3 (`this-wk`, `next-wk`, plus an "All"
  aggregate from `spot-exposures/strike`).
- **Per-expiry map:** `spot-exposures/expiry-strike?expirations[]=<e>` with
  **near-spot `min_strike`/`max_strike` (±20%) + `limit=500`** (the Phase-1.x
  lesson). Each greek returns `_oi` and `_vol` variants → the toggle selects
  columns; no hand-rolled weighting. Keep call_gamma & put_gamma SEPARATE for the
  diverging bars; `net = call + put` (signed) reused from the gex fix.
- **Regime / flip / walls per expiry:** reuse the corrected logic (net-γ sign at
  spot = regime; flip = nearest-spot zero-crossing with `no_flip` status; call
  wall = max call-γ above spot, put wall = max put-γ below).
- **Drift:** `greek-exposure/expiry` → charm/vanna + `dte`. `drift_available`
  false (and panel hidden) if the tier doesn't return them — verify at build.
- **max-pain** per expiry from `max-pain`.

## Route — `GET /api/tile3/{ticker}`

- Public (same as `/snapshot.json`); derives `flow_spot`/`direction` from the
  cached snapshot row (falls back to chain spot). Cached ~60s.
- Returns the payload above, or `{"status":"unavailable"}` when greek-exposure
  data is missing for the ticker.

## New UW wrappers (`uw.py` + `storage.py`)

- `fetch_spot_exposures_expiry_strike(ticker, expirations, min_strike, max_strike, limit=500)`
- `fetch_greek_exposure_expiry(ticker)`
Both go through the storage read-through + budget guard.

## Frontend (frontend-design, 2× footprint)

- Tile 3 spans 2 grid cells. Layout: regime banner → `[OI|Volume]` +
  `[this-wk|next-wk|All]` toggles → two-column body (left: diverging ladder —
  calls right / puts left, separated; right: levels panel + charm/vanna gauges
  with `dte`) → how-to-read + if-then + provenance.
- **Scale fix:** y-axis spans the actual rendered strike range with even spacing;
  thin labels to avoid collision. (Fixes the bunching Phase-1 ladder showed.)
- **Toggle = column select** (`_oi`/`_vol`); **expiry = re-render** from payload.
- **Loading:** render the snapshot lightweight ladder immediately; swap to rich
  on fetch. Hide the drift panel when `drift_available` is false.
- Respects the relaxed HTML guardrail; update `test_html_preservation` as needed.

## Hard constraints (from the spec)

OI/Vol = `_oi`/`_vol` columns (no hand weighting); per-view bar scaling; regime =
net-γ sign at spot per expiry (surface the this-wk-vs-next-wk flip, don't
average); charm/vanna are conditional drift gauges (hide if empty); gamma walls
primary, raw-OI walls secondary; no dark pool / shorts / flow-direction here.

## Testing

- TDD backend: `build_tile3_detail` per-expiry views; OI vs Vol column selection;
  flip/wall/regime reuse; `drift_available` toggling; near-spot window+limit sent.
- Route: auth/shape; unavailable path.
- Frontend: live cross-ticker verification + screenshots; `test_html_preservation`
  updated deliberately.

## Build checkpoints

1. UW + storage wrappers for the two new endpoints (TDD).
2. `tile3_detail.build_tile3_detail` + the gex reuse (TDD).
3. `GET /api/tile3/{ticker}` route (TDD).
4. Frontend: 2× layout, toggles, expiry filter, drift panel, scale fix.
5. Deploy; verify across tickers + the no-data path; screenshots.

## Out of scope

Tile 4 (separate spec/cycle). "All-expiries" aggregate beyond the existing
`spot-exposures/strike` map. Historical expiry playback.
