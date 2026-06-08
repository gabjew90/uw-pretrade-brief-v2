# Derive Stage + Walking Skeleton — Design (v3, Phase 3 & 4)

**Status:** PLAN (awaiting approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** Phase 0 (contracts), Phase 1 (clock), Phase 2 (golden bronze + sign findings)
**Starting point:** `server/pipeline/derive.py` (registry stub exists)

## Purpose
Turn canonical records into typed `Signal`s via **pure functions** — `canonical in →
Signal out`, NO I/O. Time is injected (from the clock) so functions stay deterministic
and golden-testable. This is the layer that catches sign inversions before they ship.

## Contract
`derive_all(canon: dict, *, asof) -> dict[str, Signal]` runs every registered
`derive_<name>(canon, *, asof) -> Signal`. A signal whose inputs are missing returns a
`Signal(provenance.quality=unavailable)` — never a fabricated value. Output is consumed
**by name** in decide.

**Purity is enforced:** no `requests`/`storage`/`datetime.now()`/network imports in
`derive.py` or any `derive_*` module (Phase 6 CI lint asserts this). All "now"-dependent
logic (session, settlement) comes via `asof`/clock values passed in.

## Phase 3 — the walking skeleton: `direction`
The single slice that proves all five boundaries. Build ONLY this signal first.

- **Input:** `list[FlowAlert]` (normalized flow-alerts for the ticker).
- **Function:** `derive_direction(canon, *, asof) -> Flow` — port the logic from
  `e1d6c5e:server/gates.py::derive_direction`:
  opening flow (`volume_oi_ratio > 1`) premium by side → `calls`/`puts` +
  `direction_basis = opening_flow`; fall back to total signed flow (`total_flow`); final
  fallback flagged. No gamma fallback in the skeleton (that needs DealerGamma — Phase 4).
- **Output:** `Flow{direction, direction_basis, call_prem, put_prem, provenance}`
  (fields per Phase-0 contract; provenance from the FlowAlerts' provenance).
- **Tests (golden + property):**
  - golden-fixture: against the Phase-2 flow-alerts bronze, assert a **sane non-None**
    `direction` ∈ {calls,puts} and the expected basis (not just "field exists").
  - property: if opening call-prem > put-prem then direction == calls (sign invariant);
    symmetric for puts; empty flow → `quality=unavailable`, not a guessed side.
- **End-to-end:** ingest(flow-alerts)→normalize(FlowAlert)→derive_direction→decide
  (trivial: action mirrors direction)→present(one Element)→frontend renders it.
- **REPLAY parity:** the same bronze yields the identical `Flow` + ViewModel offline.

## Phase 4 — remaining signals (each a pure fn, golden-tested, wired by name)
Add one at a time; each lands with its golden fixture + invariant test and is registered
+ consumed by name in decide. **Math/thresholds are DEFERRED** to the keeper modules +
research briefs (below); this spec fixes only the contract + the invariant each must hold.

| Signal | Canonical input(s) | Keeper to port | Invariant test (sign/shape) |
|---|---|---|---|
| `conviction` | GreekFlowSeries | `e1d6c5e:server/greek_flow.py` | greek-flow sign pinned in Phase 2 (event check); per-minute sum/cumsum; degenerate→unavailable |
| `dealer_gamma` | GammaExposure (spot-exposures/strike) | `e1d6c5e:server/gex.py` | flip = nearest cumulative-net crossing; `gex_sign` sign; walls call/put separate |
| `skew` | SkewPoint (historical-risk-reversal-skew) | `e1d6c5e:server/verdict.py` (`extract_vendor_rr`) | vendor RR = put−call → negated; |RR|<thr → neutral; asymmetric oppose-veto |
| `cost` | IVTermPoint + EarningsEvent + clock event window | `e1d6c5e:server/gates.py` (`_cost_gate`) | earnings<Nd or macro-in-window → block; IVR bands |
| `positioning` | OISnapshot history (from archive/silver) | `e1d6c5e:server/verdict.py` (`positioning_leg`) + `gates.py` OI gate | near-expiry OI build vs unwind; archive-decoupled → unconfirmed never blocks |
| `regime` | market-wide (SPY gamma, market-tide, econ-calendar) | `e1d6c5e:server/market_regime.py` | **NEVER a direction call** (assert no calls/puts output) |

**Known fragilities to carry as explicit tests (from review):**
- greek-flow **directional** sign is not self-validating — pin via the Phase-2 event
  check, and surface conviction cautiously (the v2 lesson). Cross-check vs net-prem-ticks.
- v2 dead code to NOT port: `flow_composite_score` percentile path (field never set);
  `crush_risk` (always False because trend was always None).

## Acceptance criteria
- [ ] `direction` end-to-end (Phase 3) per the plan's Phase-3 gate.
- [ ] Each Phase-4 signal: pure (CI-asserted), golden value test + invariant test,
      registered, consumed by name in decide, `unavailable` on missing inputs.
- [ ] No `derive_*` module imports I/O.

## Definition of done (universal)
Typed in/out; pure; golden + invariant tests assert a sane value; provenance attached;
REPLAY-reproducible.

## Defers to operator
All thresholds/parameters and which research definition applies (signal-honesty,
skew-positioning, greek-flow-delta, market-regime briefs in `e1d6c5e:docs/superpowers/`)
— filled per signal after approval. No invented thresholds; no alpha claims.

## Open questions / flags
- `positioning` reads OI **history** — that's the one signal needing the storage
  archive (silver/bronze OI), not a single canonical record. Confirm it consumes a
  derived OI-history view (built by a small silver step) rather than raw bronze.
- net-prem-ticks: use only as the greek-flow sign cross-check, or as its own signal?
  (See plan §Operator flags #2.)
