# Skew Leg + Positioning Verdict (signal-honesty Plan 3) — Design

**Date:** 2026-06-06
**Scope:** Implement the deferred Plan 3 of the signal-honesty project: wire a 25-delta risk-reversal **skew** leg as an asymmetric third directional input, collapse Flow+OI into one **Positioning** leg, demote Cost to a non-directional guard, and add a `signal_conflict` flag — surfaced as a **deep-dive-only** `row.verdict`. Supersedes the Plan 3 section of `docs/superpowers/specs/2026-06-04-signal-honesty-design.md` (lines 22–82) with three changes forced by what shipped/was probed since.

**What changed since the 2026-06-04 design (and why this is a refresh, not a rewrite):**
1. **Opening flow** is now `volume_oi_ratio>1`, not `all_opening_trades` (that flag is ~always False on Basic; fixed in commit 9e40fed). The Positioning leg uses the corrected proxy.
2. **basic-platform shipped** (request-driven, light grid). The grid is now a flow-only triage; the full read happens on click. So the 3-leg verdict is **deep-dive only**, modeled **additively** (`row.verdict`) — the existing `Gates` and the light grid are left untouched (no rename ripple).
3. **No risk-reversal endpoint exists** (probed 2026-06-06: `/api/stock/{t}/historical_risk_reversal_skew` and every candidate route 404). Skew is therefore **derived** from the `greeks` endpoint (which the deep-dive already fetches for Tile 4), which also makes the **sign known by construction** — eliminating the original spec's probe-gated sign TODO.

**Not in scope:** changing the light grid / `Gates` schema; the realized_vol regime fix (separate, independent commit); the thin daily writer.

## Why
The 4-gate model presents four peer lights but ~two real signal families: Flow and OI are both the signed-flow/positioning family; the orthogonal third family — 25Δ risk-reversal skew — is computable but never used directionally. That's manufactured confluence. Plan 3 makes the verdict honest: one Positioning leg (the predictive core), Structural and Skew as corroborate/conflict inputs (Skew asymmetric — primarily an oppose-veto), Cost as a guard, and an explicit conflict flag when the legs disagree.

## Architecture

### `server/verdict.py` (NEW, pure)
Isolated, heavily unit-tested, no I/O. Inputs are already-extracted row fields + greeks rows; output is the `Verdict`.

- `derive_rr25(greeks_rows: list[dict]) -> float | None` — 25Δ risk reversal = `call_volatility` at the strike whose `call_delta` is nearest **+0.25**, minus `put_volatility` at the strike whose `put_delta` is nearest **−0.25**. Returns `None` when the chain lacks strikes within a delta tolerance of ±0.25 on either side (too sparse / too-short expiry). All UW numerics are strings → cast.
  - **Sign (known by construction):** `RR₂₅ > 0` → 25Δ calls richer than puts → **call-skew** (bullish lean); `RR₂₅ < 0` → puts richer → **put-skew** (defensive; the index norm).
- `skew_state(rr25: float | None, direction: str, *, thr: float = _SKEW_THR) -> str` — returns `"agree" | "oppose" | "neutral" | "unavailable"`:
  - `unavailable` when `rr25 is None`.
  - For `direction=="calls"`: `agree` if `rr25 >= thr`; `oppose` if `rr25 <= -thr`; else `neutral`.
  - For `direction=="puts"`: `agree` if `rr25 <= -thr`; `oppose` if `rr25 >= thr`; else `neutral`.
  - `_SKEW_THR` (vol points, e.g. **0.02** = 2 IV points) — conservative start; calibratable. A small RR is noise, not a signal.
- `positioning_leg(direction_basis, flow_gate, oi_confirmation) -> str` ("green"/"yellow"/"red"):
  - **green** = aligned opening flow drove the side (`direction_basis in {opening_flow, total_flow}`) AND the flow gate is green — **even when** `oi_confirmation == "unconfirmed"` (decoupled from the archive; opening flow alone is green-eligible).
  - **yellow** = weak/mixed flow (flow gate yellow), OR green-strength flow but `oi_confirmation == "unwinding"` (positions being closed against the flow).
  - **red** = flow gate red / `direction_basis == "gamma_fallback"` (no real flow signal).
- `compute_verdict(*, direction, direction_basis, flow_gate, structural_gate, oi_confirmation, rr25, cost_gate) -> Verdict` — assembles the legs + `signal_conflict` + `overall`:
  - `positioning = positioning_leg(...)`, `structural = structural_gate` (already POS-capped upstream), `skew = skew_state(rr25, direction)`, `cost_guard` derived from `cost_gate` (green→ok, yellow→caution, red→block).
  - `signal_conflict = True` when Positioning is directional (green/yellow) AND (`structural == "red"` OR `skew == "oppose"`); `conflict_legs` lists which.
  - `overall`: **Favorable** = positioning green AND not signal_conflict AND cost_guard ok AND skew != oppose; **Stand down** = positioning red OR cost_guard block; **Mixed** = everything else (incl. any conflict, skew oppose, or cost caution). **Skew `agree` never upgrades to Favorable on its own** — it's corroboration, not a peer green.

### `server/schema.py`
- `Verdict(BaseModel)`: `positioning: Literal["green","yellow","red"]`, `structural: Literal["green","yellow","red"]`, `skew: Literal["agree","oppose","neutral","unavailable"]`, `cost_guard: Literal["ok","caution","block"]`, `signal_conflict: bool = False`, `conflict_legs: list[str] = []`, `overall: Literal["Favorable","Mixed","Stand down"]`, `rr25: float | None = None`.
- `Row` gains `verdict: Verdict | None = None` (None on light rows / when not computed).

### `server/snapshot.py`
- `build_single_row` (the full click build) computes `row.verdict`: it already has `direction`, `direction_basis`, `gates` (flow/structural/cost), and Tile 2's OI `confirmation`. It additionally fetches `greeks` for the near-term (hold-window) expiry via the cached storage layer — **the same fetch Tile 4 makes**, so it's typically a cache hit (greeks TTL 300s; ~0 net new calls). Expiry selection reuses the nearest-expiry logic already used for the detail tiles. `derive_rr25(greeks_rows)` → `compute_verdict(...)` → `row.verdict`.
- Light rows (`build_light_snapshot`) do **not** set `verdict` (stays None) — verdict is deep-dive only.

### `static/index.html` (deep-dive only; grid untouched)
- A **3-leg verdict panel** in the deep-dive header/Tile area: Positioning · Structural · Skew, with **Skew visually subordinate** (smaller/muted — never styled as a peer green dot), plus the Cost guard shown separately and a **loud `signal_conflict` banner** ("Flow says calls; structural/skew disagree") naming `conflict_legs`. `overall` shown as the headline verdict word.
- No change to `renderWatchlist` / the light grid.

## Error handling / honesty
- `rr25 None` (sparse chain) → `skew="unavailable"`, rendered as "skew n/a (chain too thin)", never guessed.
- Missing greeks (UWFailure) → `skew="unavailable"`; verdict still computes from Positioning + Structural + Cost.
- `signal_conflict` is informational + caps `overall` to at most Mixed; it never silently flips the side (Positioning owns the side).

## Testing (TDD, mostly against `server/verdict.py` pure functions)
- `derive_rr25`: picks the nearest-25Δ call/put strikes and returns `call_vol − put_vol`; returns None when no strike within delta tolerance on a side; handles string numerics.
- `skew_state`: calls+RR>thr→agree, calls+RR<−thr→oppose, |RR|<thr→neutral, None→unavailable; puts mirror-imaged.
- `positioning_leg`: green on opening_flow + green flow with `unconfirmed` (decoupling guard); `unwinding` caps green→yellow; gamma_fallback→red.
- `compute_verdict`: skew `oppose` → overall ≤ Mixed and signal_conflict True; skew `agree` alone never yields Favorable (no triple-confirmation); cost block → Stand down; structural red + directional positioning → signal_conflict.
- `build_single_row`: sets `row.verdict`; light rows leave it None; greeks fetch is the cached/shared one (assert no double live call when Tile 4 also runs — cache hit).
- Frontend: deep-dive renders the 3-leg panel with skew subordinate + conflict banner; grid unchanged (html-preservation stays green). Verify offline via replay + screenshot.

## Honest caveats
- **Derived skew ≈ but isn't a vendor RR.** Nearest-strike-to-25Δ interpolation is a good-enough proxy; document that it's derived (not a UW skew product). The magnitude threshold `_SKEW_THR` is a calibration to tune once we see live distributions.
- **Greeks reliability** is the same data-class risk as other UW fields; `unavailable` degrades honestly.
- **Positioning↔archive decoupling** (green without `building` confirmation) is load-bearing so the leg survives whether or not the thin daily writer ever ships.
