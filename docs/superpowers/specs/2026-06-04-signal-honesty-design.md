# Signal Honesty — Design (#1, #4, #5, #6 + opening-flow)

**Date:** 2026-06-04
**Scope:** Make the dashboard's verdict reflect the research-grounded signals correctly. Today the call/put side is gamma-derived (and can contradict the flow shown to the user), the strongest predictive signal (opening vs closing flow) is computed but never gates, the regime fact (gamma sign) never enters the structural gate, two correlated signals (flow + OI) are shown as four peer lights, and gate logic is computed twice (server Python + client JS, with a contradicting hardcode). This fixes all of that as one cohesive change to the SIGNAL layer.

**Coupling with the data-layer decision (#2) — NOT independent, resolved by graceful degradation.** The OI `building`/`unwinding` confirmation requires settled-session OI history from the archive (`confirmation = "unconfirmed"` when "not enough settled archive history yet"). The deferred #2 spec proposes deleting most of that archive under live-on-demand. To keep the two decisions decoupled, **the Positioning leg must NOT require `building` to reach green** — strong aligned opening flow alone is green-eligible; confirmation is a *bonus* (and `unwinding` is a *cap-down*), never a green prerequisite. This way the leg behaves correctly whether or not #2 keeps the archive. (If #2 ships the thin daily OI writer, confirmation still works as a bonus; if it doesn't, Positioning degrades to opening-flow-only, not to permanent-yellow.)

**Not in scope:** the basic-platform / live-on-demand re-architecture (#2), exit reference (#3), cleanup cluster (#7). Each is its own spec — but see the explicit Cost-guard ownership of the expected-move-vs-cost test below so it isn't orphaned at the #3 boundary.

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
   - **Confirmation is a modifier, NOT a green prerequisite (decouples from #2):** OI `building` strengthens an already-aligned read; `unwinding` is a cap-down toward caution; `flat`/`unconfirmed` (e.g., no archive) is *neutral* — it must NOT block green.
   - **Gate color:**
     - **green** = strong aligned opening flow (high `opening_pct`, clear premium imbalance) — reachable on flow alone; `building` is a bonus, not required.
     - **yellow** = weak/mixed opening flow, or strong flow with `unwinding` confirmation (flow says one thing, positioning is being closed).
     - **red** = opening flow opposite the displayed side.

2. **Structural** (gamma) — flip + walls, **now reading `gex_sign` (#4)**:
   - `gex_sign=="POS"` (positive gamma, chop/pin, premium-killing): **cap the leg at YELLOW** for a directional weekly, regardless of flip/wall alignment.
   - `gex_sign=="NEG"` (negative gamma, trend): existing flip/wall logic can reach green.
   - `gex_status=="unavailable"`: neutral (yellow), never green (existing honesty state).

3. **Skew** (NEW directional leg) — 25-delta risk-reversal (#5). **Asymmetric by design — it is primarily an OPPOSE-veto, not a co-equal third confirmation.** Skew agreeing with the side is *mild corroboration*; skew opposing the side is a *meaningful caution*. Rendering skew-agreement as a third full green light would re-import the confluence theater this project removes — so the UI must NOT present "3 greens" as independent triple-confirmation when one green is only mild skew agreement.
   - Fetched per row build (`fetch_risk_reversal_skew`; currently only in Tile 4). Adds ~1 call/ticker to the build.
   - **Sign convention MUST be confirmed against a live payload before trusting it** (probe approach as used for greeks/atm). Working assumption: positive RR = calls priced over puts = bullish → supports calls; negative → supports puts.
   - **Gate behavior:** **red/caution** when skew clearly opposes the Positioning side (the load-bearing case); **neutral-with-a-tick** when it agrees (shown as mild corroboration, visually subordinate — not a peer green light); **neutral** when flat/mild. Skew never *creates* a green verdict on its own.

   **Cost** stays as a separate **non-directional guard** — rendered distinctly, NOT as a peer directional "light." **This spec OWNS the expected-move-vs-cost test** (assigned here explicitly so it isn't orphaned at the #3 boundary): the Cost guard checks spread/IV/event AND **"can the expected move clear the round-trip cost"** — i.e., does the at-the-money expected move (already computed in Tile 4) exceed spread + an estimate of theta paid over a few-day hold. If the move can't pay for the trade, the Cost guard flags it regardless of how green the directional legs are. (#3's exit reference will *reuse* these same expected-move/breakeven numbers for the target/time-stop, but the entry-viability test lives here.)

### Direction derivation — #1 + opening-flow

- **All tickers (no index special-case):** `direction` = sign of net OPENING flow (`opening_call_prem − opening_put_prem`).
- **Trust-but-verify the opening input.** `all_opening_trades` is now the load-bearing *direction* input, and it is the same data-reliability risk class as the documented ask-side / `net_prem_ticks=0` tier gotchas. Before leaning on it: add a **population probe + contract test** (does it actually populate on the Basic tier, and how often?), exactly as we gate the skew sign convention. If opening data is frequently absent, the tool is mostly running `total_flow` (Pan–Poteshman) rather than opening flow (Ge–Lin–Pearson) — a materially weaker signal, and we must not pretend otherwise.
- **Fallback cascade** when opening data is absent: net signed TOTAL flow → then the prior gamma rule (documented last-resort), never silently. Direction carries a `direction_basis` field ("opening_flow" | "total_flow" | "gamma_fallback").
- **`direction_basis` is USER-FACING, not just logged.** The UI surfaces it and visibly marks `total_flow` and `gamma_fallback` as the *weaker* cases (the call is being made on a degraded basis) — same honesty principle as the freshness `as_of` line.
- Computed **server-side in the gate/build path** (moved out of the ad-hoc `snapshot.py:226` block into the single source of truth).
- **Index note:** dropping the SPY/QQQ/IWM special-case is safe *because* the new `gex_sign` yellow-cap (#4) already does its main job — positive-gamma chop on an index caps the structural leg at yellow regardless of flow, so the uniform opening-flow rule behaves correctly across index regimes. Residual (minor, deferred): market-tide is not used as index confirmation anywhere.

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

- `gates.py`: opening-flow direction (opening vs closing premium drives the side; closing-only flow does NOT set direction); fallback cascade + `direction_basis`; positive-gamma caps structural at yellow; `signal_conflict` set when legs disagree AND flow still picks the side under conflict.
- **Decoupling test (load-bearing):** Positioning reaches **green on strong aligned opening flow with `confirmation="unconfirmed"`** (no archive) — proves independence from #2. And `unwinding` caps an otherwise-green Positioning to yellow.
- **Skew asymmetry test:** skew *opposing* the side → caution/red; skew *agreeing* does NOT by itself produce a green verdict (no triple-confirmation from skew alone).
- **Opening-input probe:** population probe + contract test for `all_opening_trades` (does it populate on tier; how often) — mirrors the skew-sign probe. `direction_basis` defaults honestly when absent.
- **Cost guard:** expected-move-vs-round-trip-cost test (move below cost → Cost flags regardless of directional greens).
- **Skew sign convention:** live-payload probe + contract test pinning the field used.
- **Frontend:** assert the client renders `row.gates`/`row.direction` only — no `CALIBRATION` gate recomputation, no `gates.oi="yellow"` hardcode (guard test); `direction_basis` weaker-case marking renders; skew agreement renders visually subordinate (not a peer green). Verify offline via replay + screenshot of the 3-leg panel + disagree flag.

## Honest trade-offs

- The VERDICT changes — tickers gate differently (more honestly). Expect **fewer all-green** rows (reduced confluence theater) — that is the intended effect, not a regression.
- Skew as a directional leg depends on the RR sign convention, which is **unconfirmed until probed live**. The plan gates the skew leg behind that confirmation.
- Adds ~1 UW call/ticker (risk-reversal) to the build. Negligible.
