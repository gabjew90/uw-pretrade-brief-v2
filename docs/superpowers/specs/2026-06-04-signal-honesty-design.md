# Signal Honesty — Design (#1, #4, #5, #6 + opening-flow)

**Date:** 2026-06-04
**Scope:** Make the dashboard's verdict reflect the research-grounded signals correctly. Today the call/put side is gamma-derived (and can contradict the flow shown to the user), the strongest predictive signal (opening vs closing flow) is computed but never gates, the regime fact (gamma sign) never enters the structural gate, two correlated signals (flow + OI) are shown as four peer lights, and gate logic is computed twice (server Python + client JS, with a contradicting hardcode). This fixes all of that as one cohesive change to the SIGNAL layer. Independent of the data-layer/basic-platform decision (deferred to a separate project).

**Not in scope:** the basic-platform / live-on-demand re-architecture (#2), exit reference (#3), cleanup cluster (#7). Each is its own spec.

## Problem (verified against code)

- **Direction is gamma-derived** (`snapshot.py:226`): `if gex_sign=="NEG" or flip_pct>0: "calls" else "puts"`. Negative gamma amplifies moves in *either* direction (the code's own comment concedes this), so `NEG→calls` carries no up-bias. The contract picker can rank calls while Tile 1 shows put-buying — a self-contradicting, wrong-way signal. The signed flow is computed and then discarded.
- **Opening-flow read never gates.** `all_opening_trades`, `opening_pct`, and `confirmation` (building/unwinding) are computed in `snapshot.py` and shown in Tile 2, but `gates.py` reads NONE of them (grep-confirmed). The Ge–Lin–Pearson crux — opening bets predict, closing bets don't — is decorative.
- **`gex_sign` never enters the structural gate** (`gates.py` `_structural_gate` reads flip/wall distance only; `gex_sign` absent from the whole file). It can flash green in a positive-gamma pin regime.
- **Four peer lights, ~two real signals.** Flow and OI are both the signed-flow/positioning family. The orthogonal third family — 25-delta risk-reversal skew — is fetched but used only as a Tile 4 cost check, never directionally. Manufactured confluence.
- **Two sources of truth.** Gates computed in `gates.py` (`GATE_THRESHOLDS`) AND in `index.html` (`CALIBRATION`), with `index.html:1279` hardcoding `gates.oi = "yellow"` (contradicting the server's OI gate) and line 1299 recomputing it.

## Design

### Three honest legs (replaces 4 peer lights) — #5

1. **Positioning** (collapses Flow + OI into the opening-flow read — the predictive core):
   - **Side = signed OPENING flow:** from `flow_alerts_detail`, sum `total_premium` for `all_opening_trades==True`, split by `type` (call/put). `opening_call_prem` vs `opening_put_prem` sets the side. **Closing trades excluded.**
   - **Strength:** `opening_pct` (share of flow that is opening) + opening-premium magnitude.
   - **Confirmation:** OI `building` (aligned → green-eligible), `unwinding` (→ caps at red/caution), `flat`/`unconfirmed` (→ yellow).
   - **Gate color:** green = strong aligned opening flow (high `opening_pct`) + building; yellow = weak/mixed/flat; red = opening flow opposite the displayed side OR unwinding.

2. **Structural** (gamma) — flip + walls, **now reading `gex_sign` (#4)**:
   - `gex_sign=="POS"` (positive gamma, chop/pin, premium-killing): **cap the leg at YELLOW** for a directional weekly, regardless of flip/wall alignment.
   - `gex_sign=="NEG"` (negative gamma, trend): existing flip/wall logic can reach green.
   - `gex_status=="unavailable"`: neutral (yellow), never green (existing honesty state).

3. **Skew** (NEW directional leg) — 25-delta risk-reversal (#5):
   - Fetched per row build (`fetch_risk_reversal_skew`; currently only in Tile 4). Adds ~1 call/ticker to the build.
   - **Sign convention MUST be confirmed against a live payload before trusting it** (probe approach as used for greeks/atm). Working assumption: positive RR = calls priced over puts = bullish → supports calls; negative → supports puts.
   - **Gate color:** green = skew direction agrees with the Positioning side AND magnitude is meaningful; yellow = flat/mild; red = skew opposes the side.

   **Cost** stays as a separate **non-directional guard** (spread/IV/event) — rendered distinctly, NOT as a peer directional "light."

### Direction derivation — #1 + opening-flow

- **All tickers (no index special-case):** `direction` = sign of net OPENING flow (`opening_call_prem − opening_put_prem`).
- **Fallback cascade** when opening data is absent: net signed TOTAL flow → then the prior gamma rule (documented last-resort), never silently. Direction always carries a `direction_basis` field ("opening_flow" | "total_flow" | "gamma_fallback") for honesty.
- Computed **server-side in the gate/build path** (moved out of the ad-hoc `snapshot.py:226` block into the single source of truth).

### Disagreement flag

- When the Positioning side conflicts with the Structural and/or Skew read, set `row.signal_conflict = True` (+ which legs disagree).
- **Behavior (operator choice):** opening flow STILL sets the direction (it's the predictive signal), but the structural leg caps at yellow and a prominent **"signals disagree"** flag renders. Never let gamma silently win.

### One source of truth — #6

- `server/gates.py` computes ALL leg colors + `direction` + `direction_basis` + `signal_conflict` → on `row.gates` / `row.direction`.
- The client **renders only**: remove the JS `CALIBRATION` gate recomputation and the `gates.oi = "yellow"` hardcode (`index.html`). The client reads `row.gates`/`row.direction`. `CALIBRATION` entries used purely for display formatting may stay; the gate-DECISION thresholds move out.

## Schema changes

- `Row.direction_basis: str` ("opening_flow" | "total_flow" | "gamma_fallback").
- `Row.signal_conflict: bool` (+ optional `conflict_detail`).
- `Gates` gains/renames legs to reflect the 3-leg structure: `positioning` (replaces flow+oi as the directional family), `structural`, `skew` (new), plus `cost` kept as a non-directional guard. (Exact field naming finalized in the plan; keep back-compat shims if the frontend needs a transition.)

## Testing (TDD)

- `gates.py`: opening-flow direction (opening vs closing premium drives the side; closing-only flow does NOT set direction); fallback cascade + `direction_basis`; positive-gamma caps structural at yellow; skew leg agrees/opposes; Positioning combines opening-flow + building/unwinding; `signal_conflict` set when legs disagree; flow still picks the side under conflict.
- Skew sign convention: a live-payload probe + a contract test pinning the field used.
- Frontend: assert the client renders `row.gates` only — no `CALIBRATION` gate recomputation, no `gates.oi="yellow"` hardcode (guard test). Verify offline via replay + screenshot of the 3-leg panel + disagree flag.

## Honest trade-offs

- The VERDICT changes — tickers gate differently (more honestly). Expect **fewer all-green** rows (reduced confluence theater) — that is the intended effect, not a regression.
- Skew as a directional leg depends on the RR sign convention, which is **unconfirmed until probed live**. The plan gates the skew leg behind that confirmation.
- Adds ~1 UW call/ticker (risk-reversal) to the build. Negligible.
