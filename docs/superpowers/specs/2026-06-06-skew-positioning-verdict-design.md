# Skew Leg + Positioning Verdict (signal-honesty Plan 3) — Design

**Date:** 2026-06-06
**Scope:** Implement the deferred Plan 3 of the signal-honesty project: wire a 25-delta risk-reversal **skew** leg as an asymmetric third directional input, collapse Flow+OI into one **Positioning** leg, demote Cost to a non-directional guard, and add a `signal_conflict` flag — surfaced as a **deep-dive-only** `row.verdict`. Supersedes the Plan 3 section of `docs/superpowers/specs/2026-06-04-signal-honesty-design.md` (lines 22–82) with three changes forced by what shipped/was probed since.

**What changed since the 2026-06-04 design (and why this is a refresh, not a rewrite):**
1. **Opening flow** is now `volume_oi_ratio>1`, not `all_opening_trades` (that flag is ~always False on Basic; fixed in commit 9e40fed). The Positioning leg uses the corrected proxy.
2. **basic-platform shipped** (request-driven, light grid). The grid is now a flow-only triage; the full read happens on click. So the 3-leg verdict is **deep-dive only**, modeled **additively** (`row.verdict`) — the existing `Gates` and the light grid are left untouched (no rename ripple).
3. **The risk-reversal endpoint EXISTS** — the earlier "404" was a path typo (underscores vs UW's hyphens; corrected to `/api/stock/{t}/historical-risk-reversal-skew`). Skew uses the **vendor** RR as primary (`extract_vendor_rr`, sign-corrected: vendor `risk_reversal` = put−call, NEGATED to the call−put convention — pinned by cross-check vs the greeks-derived RR for the same expiry), with the **greeks-derived `derive_rr25`** (sign known by construction) as the **fallback** when vendor is unavailable. [Correction committed in 4315ffa, after the initial greeks-only design.]

**Not in scope:** changing the light grid / `Gates` schema; the realized_vol regime fix (separate, independent commit); the thin daily writer.

## Why
The 4-gate model presents four peer lights but ~two real signal families: Flow and OI are both the signed-flow/positioning family; the orthogonal third family — 25Δ risk-reversal skew — is computable but never used directionally. That's manufactured confluence. Plan 3 makes the verdict honest: one Positioning leg (the predictive core), Structural and Skew as corroborate/conflict inputs (Skew asymmetric — primarily an oppose-veto), Cost as a guard, and an explicit conflict flag when the legs disagree.

## Architecture

### `server/verdict.py` (NEW, pure)
Isolated, heavily unit-tested, no I/O. Inputs are already-extracted row fields + greeks rows; output is the `Verdict`.

- `derive_rr25(greeks_rows: list[dict]) -> float | None` — 25Δ risk reversal = `call_volatility` at the strike whose `call_delta` is nearest **+0.25**, minus `put_volatility` at the strike whose `put_delta` is nearest **−0.25**. Returns `None` when the chain lacks strikes within a delta tolerance of ±0.25 on either side (too sparse / too-short expiry). All UW numerics are strings → cast.
  - **Sign (known by construction):** `RR₂₅ > 0` → 25Δ calls richer than puts → **call-skew** (bullish lean); `RR₂₅ < 0` → puts richer → **put-skew** (defensive; the index norm).
  - **Probe-confirmed (2026-06-06, live SPY):** the `greeks` payload carries per-strike `call_delta`/`put_delta`/`call_volatility`/`put_volatility`, **populated at the 25Δ wings** (e.g. weekly: 25Δ call δ0.241 IV0.173, 25Δ put δ−0.248 IV0.229 → RR₂₅ −0.057; ~30d: −0.060). Negative RR for SPY = put-skew, matching the index norm — the field-shape risk the reviewer flagged is empirically retired. A real greeks payload is captured as a golden fixture for the `derive_rr25` test.
- `skew_state(rr25: float | None, direction: str, *, thr: float = _SKEW_THR) -> str` — returns `"agree" | "oppose" | "neutral" | "unavailable"`:
  - `unavailable` when `rr25 is None`.
  - For `direction=="calls"`: `agree` if `rr25 >= thr`; `oppose` if `rr25 <= -thr`; else `neutral`.
  - For `direction=="puts"`: `agree` if `rr25 <= -thr`; `oppose` if `rr25 >= thr`; else `neutral`.
  - `_SKEW_THR` (vol points, e.g. **0.02** = 2 IV points) — conservative start; calibratable. A small RR is noise, not a signal.
- `positioning_leg(direction_basis, flow_gate, oi_confirmation) -> str` ("green"/"yellow"/"red"):
  - **green** = the side came from **`opening_flow`** specifically AND the flow gate is green — **even when** `oi_confirmation == "unconfirmed"` (decoupled from the archive; opening flow alone is green-eligible). Green requires the *opening* basis, not the weaker fallback.
  - **yellow** = `direction_basis == "total_flow"` (the weaker, non-opening fallback — capped here so a Favorable verdict can't rest on it), OR weak/mixed flow (flow gate yellow), OR green-strength opening flow but `oi_confirmation == "unwinding"` (positions being closed against the flow).
  - **red** = flow gate red / `direction_basis == "gamma_fallback"` (no real flow signal).
  - **Note:** the opening proxy is `volume_oi_ratio>1` (`all_opening_trades` is dead on Basic). It's a heuristic — high vol/OI also captures churn and same-day round-trips, not purely net-new directional opening. The predictive core rests on it, so it stays **labeled a proxy** in the UI ("opening-ish flow"), never overstated.
- `compute_verdict(*, direction, direction_basis, flow_gate, structural_gate, oi_confirmation, rr25, cost_gate) -> Verdict` — assembles the legs + `signal_conflict` + `overall`:
  - `positioning = positioning_leg(...)`, `structural = structural_gate` (POS-cap **verified shipped** — `gates._structural_gate` caps green→yellow when `gex_sign=="POS"`, gates.py:127-131; so the verdict's structural input is already gamma-aware), `skew = skew_state(rr25, direction)`, `cost_guard` derived from `cost_gate` (green→ok, yellow→caution, red→block).
  - `signal_conflict = True` when Positioning is directional (green/yellow) AND (`structural == "red"` OR `skew == "oppose"`); `conflict_legs` lists which.
  - `overall`: **Favorable** = positioning green (which now requires `opening_flow`) AND not signal_conflict AND cost_guard ok AND skew != oppose; **Stand down** = positioning red OR cost_guard block; **Mixed** = everything else (incl. any conflict, skew oppose, or cost caution). **Skew `agree` never upgrades to Favorable on its own** — it's corroboration, not a peer green.
  - `action` (the part a novice actually needs — posture words alone don't say what to DO, and "Mixed+conflict" is the ambiguous case where a novice drifts toward trading, against this tool's whole bias): **Favorable → "Worth acting on — the rare one"**; **Mixed + signal_conflict → "Skip — signals disagree"**; **Mixed (no conflict) → "Wait — not compelling"**; **Stand down → "Stand down"**. Both Mixed-with-conflict and Stand down read as skip/wait.

### `server/schema.py`
- `Verdict(BaseModel)`: `positioning: Literal["green","yellow","red"]`, `structural: Literal["green","yellow","red"]`, `skew: Literal["agree","oppose","neutral","unavailable"]`, `cost_guard: Literal["ok","caution","block"]`, `signal_conflict: bool = False`, `conflict_legs: list[str] = []`, `overall: Literal["Favorable","Mixed","Stand down"]`, `action: str` (the explicit do-this mapping above), `rr25: float | None = None`.
- `Row` gains `verdict: Verdict | None = None` (None on light rows / when not computed).

### `server/snapshot.py`
- `build_single_row` (the full click build) computes `row.verdict`: it already has `direction`, `direction_basis`, `gates` (flow/structural/cost), and Tile 2's OI `confirmation`. For skew it fetches `greeks` for a **~30-day expiry** (the nearest monthly ≥ ~25 DTE) — **not** the weekly hold-window expiry — because 25Δ wing IVs on a 0–7 DTE chain are jumpy/illiquid and a 2-IV-pt threshold sits inside that noise; the ~30d horizon is the sounder skew signal (Cremers-Weinbaum) and the probe confirmed it's equally liquid. **Cost:** this is a *different* expiry than Tile 4's weekly greeks, so it is **~1 extra greeks call** on the deep-dive (not a free cache hit) — negligible (deep-dive only, on click). `derive_rr25(greeks_rows)` → `compute_verdict(...)` → `row.verdict`.
- Light rows (`build_light_snapshot`) do **not** set `verdict` (stays None) — verdict is deep-dive only.

### `static/index.html` (deep-dive only; grid untouched)
- A **3-leg verdict panel** in the deep-dive header/Tile area: Positioning · Structural · Skew, with **Skew visually subordinate** (smaller/muted — never styled as a peer green dot), plus the Cost guard shown separately and a **loud `signal_conflict` banner** ("Flow says calls; structural/skew disagree") naming `conflict_legs`. The headline shows **`action`** (the do-this), with `overall` as the supporting posture word — so a novice reads "Skip — signals disagree", not just "Mixed".
- No change to `renderWatchlist` / the light grid.

## Error handling / honesty
- `rr25 None` (sparse chain) → `skew="unavailable"`, rendered as "skew n/a (chain too thin)", never guessed.
- Missing greeks (UWFailure) → `skew="unavailable"`; verdict still computes from Positioning + Structural + Cost.
- `signal_conflict` is informational + caps `overall` to at most Mixed; it never silently flips the side (Positioning owns the side).

## Testing (TDD, mostly against `server/verdict.py` pure functions)
- `derive_rr25`: against a **real greeks golden fixture** (captured from the live probe), returns `call_vol(25Δ) − put_vol(25Δ)` with the correct sign (SPY → negative/put-skew); returns None when no strike within delta tolerance on a side; handles string numerics.
- `skew_state`: calls+RR>thr→agree, calls+RR<−thr→oppose, |RR|<thr→neutral, None→unavailable; puts mirror-imaged.
- `positioning_leg`: **green only on `opening_flow`** + green flow with `unconfirmed` (decoupling guard); **`total_flow` caps at yellow** (weaker basis can't reach Favorable); `unwinding` caps green→yellow; gamma_fallback→red.
- `compute_verdict`: skew `oppose` → overall ≤ Mixed and signal_conflict True; skew `agree` alone never yields Favorable (no triple-confirmation); a `total_flow`-based row never reaches Favorable; cost block → Stand down; structural red + directional positioning → signal_conflict.
- `action`: Favorable→"Worth acting on…"; Mixed+conflict→"Skip — signals disagree"; Mixed→"Wait…"; Stand down→"Stand down".
- `build_single_row`: sets `row.verdict`; light rows leave it None; greeks fetch is the cached/shared one (assert no double live call when Tile 4 also runs — cache hit).
- Frontend: deep-dive renders the 3-leg panel with skew subordinate + conflict banner; grid unchanged (html-preservation stays green). Verify offline via replay + screenshot.

## Honest caveats
- **Derived skew ≈ but isn't a vendor RR.** Nearest-strike-to-25Δ interpolation is a good-enough proxy; document that it's derived (not a UW skew product). The magnitude threshold `_SKEW_THR` (0.02) is a calibration to tune once we see live distributions — and it's why skew reads off the **~30d** chain, not the noisy weekly wings where a 2-IV-pt move is inside the bid/ask (the probe showed SPY 30d RR −0.060, a clean multi-point signal well outside noise).
- **Greeks reliability** is the same data-class risk as other UW fields; `unavailable` degrades honestly.
- **Positioning↔archive decoupling** (green without `building` confirmation) is load-bearing so the leg survives whether or not the thin daily writer ever ships.
