# Tile 3 — Real Gamma Ladder (Phase 1) — Design

**Date:** 2026-05-29
**Scope:** Phase 1 of the Tile 3 build spec (`tile3_build_spec.md`). Make the
existing Structural-Setup ladder render **real per-strike net dealer gamma**
instead of `strHash`-synthesized bars. Explicitly defers the spec's OI/Volume
toggle, expiry filter, charm/vanna drift panel, 2× footprint, and the
`expiry-strike` / `greek-exposure` endpoints to Phase 2.

## Why Phase 1 only

The full spec is a greenfield design that collides with two constraints: the UW
budget (its `spot-exposures/expiry-strike` primary endpoint is huge ×15/cycle,
right after the 2026-05-29 budget outage) and, previously, the HTML guardrail
(now relaxed — `frontend-design` allowed). Phase 1 delivers the highest-value
honest improvement — a real gamma distribution — using data **already fetched**,
at **zero additional UW cost**.

## Current state (what exists)

- `renderTile3Structural(row)` + `ladderSvg(...)` already draw a γ ladder.
- Structural **levels are real/live**: `flip_dist_pct`, `wall_up/dn_dist_pct`,
  `gex_sign`, `agg_gamma_b` (from `snapshot._extract_gex` over
  `spot-exposures/strike`).
- The ladder **bars are synthetic**: `ladderSvg` builds them from
  `strHash(row.ticker)`. The per-strike gamma the backend already computes
  (`uw.gex_records`) is reduced to flip/walls and otherwise discarded.

## Backend

- **`schema.py`**: new `Tile3Strike{ strike: float, net_gamma: float }` and
  `Tile3{ strikes: list[Tile3Strike] }`; add `Row.tile3: Tile3 =
  Field(default_factory=Tile3)`. Empty list on warming/failure.
- **`snapshot.py`**: `_build_tile3(spot_data, spot) -> Tile3`. Reuse
  `uw.gex_records(spot_data)` (net γ per strike = call_gamma_oi − put_gamma_oi).
  Filter to the ladder window (strikes within ±8% of spot), cap to ~25 strikes
  nearest spot, sort by strike. Return empty on `UWFailure`/empty/`spot<=0`.
  Wire into `_build_dashboard_row` (we already have `spot_data` in hand).

## Frontend (`static/index.html`)

- `ladderSvg`: if `row.tile3 && row.tile3.strikes.length`, render **real bars** —
  one per archived strike, vertical position by strike, bar length ∝
  `|net_gamma|` scaled to the max within the view (per-view scaling), direction +
  color by sign (positive net γ → blue/right via `ladderGradPos`; negative →
  red/left via `ladderGradNeg`). Keep the flip/spot/call-wall/put-wall reference
  lines exactly as they are.
- **Fallback:** when `row.tile3.strikes` is absent/empty (warming, UW failure),
  keep the existing synthetic bars so the tile never looks broken.
- Quality pass via the `frontend-design` skill — clean bars, readable labels,
  consistent with the existing ladder aesthetic. No layout/footprint change.

## Out of scope (Phase 2)

OI/Volume toggle, expiry filter, charm/vanna drift panel, 2× footprint,
call-vs-put *separated* diverging bars, `spot-exposures/expiry-strike` +
`greek-exposure/expiry` endpoints. Each needs the budget + architecture
conversation (on-demand `/api/tile3/{ticker}` route is the likely budget-safe
shape).

## Testing

- **TDD (backend):** `_build_tile3` returns correct net-γ per strike from a
  `spot-exposures/strike` payload; windows/caps strikes; returns empty on
  `UWFailure` / empty / `spot<=0`. `Tile3` schema round-trips via `model_dump`.
  `refresh_snapshot` happy-path rows carry a populated `tile3`.
- **HTML preservation:** run `tests/test_html_preservation.py`; update it
  deliberately if the `ladderSvg` edit trips it (per the relaxed CLAUDE.md
  guardrail), keeping it a guard against accidental drift.
- **Visual:** verify the live ladder on the running site (real bars vs the old
  synthetic shape) after deploy.

## Rollout

Backend + frontend ship together in the per-cycle snapshot (no new endpoints, no
new env, no budget change). On deploy the cold-boot rebuild populates `tile3`;
the ladder goes real on the next render.
