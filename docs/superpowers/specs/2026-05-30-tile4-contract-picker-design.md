# Tile 4 — Contract Picker & Final Gate — Design

**Date:** 2026-05-30
**Scope:** Build the spec's "Tile 4 — Contract Picker & Final Gate" (`tile4_build_spec.md`)
as the end-of-funnel tile. Scores every realistic weekly contract in the
Tiles 1–3 direction on six checks, enforces hard event/IV-rank gates, and
surfaces ONE pick — or "stand down". On-demand, mirroring Tile 3 Phase 2.

## Decisions (locked with operator)

1. **Reuse, don't refetch.** Flow/Campaign/Room/regime/direction come from the
   snapshot row + the Tile-3 detail; Tile 4 fetches only NEW data. Scored once.
2. **Degrade honestly.** Missing data ⇒ check shows "unknown" (neutral dot), never
   a pass. A hard gate that can't be evaluated WARNS, never silently greenlights.
3. **Layout:** merge into the Picker slot — replace the current Tile 6 Picker;
   slim the old Tile 4 (Cost) to its IVR gauge so the heavy vol reads live only
   in the picker.

## Endpoint contracts (verified against UW OpenAPI)

- `greeks` — ticker + **expiry** (req). delta/theta/vega/gamma per contract.
- `atm-chains` — ticker + **expirations[]** (req). Expected move (ATM straddle).
- `historical_risk_reversal_skew` — ticker + **expiry** + **delta** (req) + timeframe.
- `volatility/realized` — ticker + timeframe.
- `stock-state` — ticker. Spot/prev-close reference.
- `fda-calendar` — date-range + ticker filter.
- **`economic-calendar` does NOT exist in the UW API.** Event gate = earnings +
  FDA; FOMC/CPI shown as "not auto-checked" (optionally surfaced from
  `REGIME_DETAIL_TEXT`). Honest-degrade, never a false greenlight.
- Reused (already wrapped): option-contracts, interpolated-iv, volatility/
  term-structure, earnings; snapshot row + Tile-3 detail for flow/OI/walls.

## Backend — `server/tile4.py`

`build_tile4(ticker, ctx) -> dict`, where `ctx` carries reused Tiles 1–3 outputs
(spot, direction, flow_strikes, oi_campaign, walls, max_pain, regime).

**Gates (veto layer):**
- `event`: earnings or FDA event before the traded expiry → **hard block**. Macro
  (FOMC/CPI) → "not auto-checked" note (no endpoint).
- `iv_rank`: > ~80 → **block** (crush risk), any direction.
- `term_structure`: front vs back-month-average reference → amber (front-rich) /
  green (front-cheap). NOT a block.

**Six-check score per contract** (fixed order: Flow · Campaign · Room · Target ·
Execution · Greeks), windowed strikes in the trade direction:
1. Flow — strike in the Tile-1 flow strikes.
2. Campaign — OI building here (Tile-2), not unwinding.
3. Room — clear distance to the nearest wall in the trade direction (Tile-3).
4. Target — breakeven move ≤ weekly expected move (atm-chains).
5. Execution — spread ≤ ~5% of premium AND IV not skew-pumped vs ATM (put
   borrow-fee caveat).
6. Greeks — delta 0.35–0.55 and theta bearable.

**Hard-fail caps:** Target & Execution gate the score — failing either caps the
displayed score so the contract can never be the ★ pick. Each dot is pass /
fail / **unknown** (missing data). **Tiebreaker:** flow-aligned → lower cost →
delta nearer 0.45.

**Stand-down:** any hard gate trips ⇒ `{status:"stand_down", reason}`, no pick.
Returns `{status:"unavailable"}` when the chain/greeks can't be fetched.

Response: `{status, gates:{event,iv_rank,term_structure}, term_curve:[...],
recommendation:{...}|null, contracts:[{strike,type,premium,delta,spread_pct,
iv_vs_atm,be_move_pct,oi,checks:{...},score,reason,pick}], direction}`.

## New UW + storage wrappers

`fetch_greeks(ticker, expiry)`, `fetch_atm_chains(ticker, expirations)`,
`fetch_risk_reversal_skew(ticker, expiry, delta=25)`, `fetch_realized_vol(ticker)`,
`fetch_stock_state(ticker)`, `fetch_fda_calendar(ticker)`. Near-spot/limit where
applicable; storage read-through + budget guard.

## Frontend (frontend-design, Picker slot)

Replace `renderTile6Picker` with the rich tile (on-demand fetch like tile3):
1. Two gate badges (Event · IV rank) — red/amber/green, "unknown" styled distinctly.
2. Term-structure SVG: IV across expiries, traded expiry marked, back-month
   reference line; panel color amber if front-rich, green if front-cheap.
3. Recommendation card (or stand-down message): contract, ask, "controls ~$Xk",
   one-line thesis from passing checks, key stats (δ/θ/spread/IV-vs-ATM/needed-vs-
   expected move).
4. Scored chain table: windowed strikes, FLOW/WALL/MAX-PAIN pills, columns
   (premium, δ, spread, IV vs ATM, breakeven, OI, six dots + reason, score), ★ best.
5. Legend + provenance. Slim Tile 4 (Cost) to the IVR gauge only.

## Testing (TDD)

Backend: each gate (block/amber/green/unknown); six checks incl. unknown dots;
Target/Execution hard-fail cap; tiebreaker order; stand-down on hard gate;
unavailable on missing chain. Route auth/shape. Endpoints send required params.
Frontend: live verify (gates, term-structure, rec card, scored table, stand-down)
+ screenshots; update test_html_preservation deliberately.

## Build checkpoints

1. uw + storage wrappers for the new endpoints (TDD).
2. `tile4.build_tile4` — gates + six-check scoring + stand-down + tiebreaker (TDD). Core.
3. `GET /api/tile4/{ticker}` route (TDD).
4. Frontend: picker-slot rich tile + slim Cost tile.
5. Deploy; verify live across tickers + stand-down path; screenshots.

## Out of scope

Multi-leg/spreads. Historical backtest of picks. The (nonexistent)
economic-calendar integration. Auto-refresh of the picker (fetched on select).
