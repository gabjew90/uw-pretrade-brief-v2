# Decide — Design (v3, Phase 4+)

**Status:** PLAN (awaiting approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** Phase 0 (contracts — `Signal`, `Verdict`); Phase 3 (Derive — `dict[str, Signal]`)
**Starting point:** `server/pipeline/decide.py` (scaffold: stub returns `Verdict` with `signals_used=sorted(signals.keys())`)

## Purpose / role

Apply the verdict funnel in **exactly one place** (the server) and emit a `Verdict`.
Consumes signals **by name** from the derive map; never re-derives, never calls I/O,
never looks up raw data. Because inputs are named dict keys, an unused signal is a
structurally visible gap (`signals_used` vs `signals.keys()` is assertable in tests).
The frontend never re-computes the verdict — that rule is enforced by the Present
stage, which forwards `Verdict` verbatim.

## Contract (typed in/out)

```
decide(signals: dict[str, Signal]) -> Verdict
```

- **Input:** `dict[str, Signal]` keyed by signal name (e.g. `"flow"`, `"positioning"`,
  `"skew"`, `"cost"`, `"structural"`, `"regime"`). Any signal may have
  `provenance.quality = unavailable` — the funnel handles graceful degradation without
  fabricating a value.
- **Output:** `Verdict{action: str, reasons: list[str], signals_used: list[str],
  provenance: Provenance}` where `signals_used` is the subset of keys actually read
  during this funnel pass; `provenance` is `Provenance.worst(*[s.provenance for s in
  consumed_signals])`.
- **Pure:** no I/O, no `datetime.now()`, no storage access. All "now"-sensitive
  information arrived via the signals (e.g. `cost.event_within_hold` from the clock-
  informed derive step).

## Responsibilities

**Decide owns:**
- The funnel order (caps, vetoes, overrides) exactly as documented in v2's
  `compute_verdict` and `positioning_leg` — ported, not reinvented.
- Consuming signals **by name** via `signals.get("flow")`, `signals.get("skew")`, etc.
  — not by position or type-check.
- Populating `signals_used` with exactly the names read during this call (not all
  available names).
- Emitting `action` and `reasons[]` in plain English. The exact copy is **deferred** to
  the operator's surface brief; the funnel logic and the structural outcome categories
  (`Favorable / Mixed / Stand down`) are fixed here.
- Degrading gracefully when a signal is unavailable: an unavailable `skew` signal
  resolves to the `neutral` path (not a block); an unavailable `cost` signal resolves
  to the `caution` path (conservative but not a hard block).

**Decide does NOT own:**
- Computing any signal value (Derive's job).
- Knowing what tile renders what (Present's job).
- Thresholds for signal quality (e.g. what IV percentile constitutes `cost.green`) —
  those live in Derive, and Decide only reads the already-computed gate color/category.
- The `regime` override surface (regime is a read — gate output — that caps or vetoes;
  it is not a direction call — assert this in tests).

## Key behaviors / edge cases

**Funnel order (port from `e1d6c5e:server/verdict.py::compute_verdict`):**

1. **Positioning cap/veto** (`positioning_leg` logic): collapses Flow + OI. Green
   requires `direction_basis == opening_flow`. `total_flow` basis caps at yellow. OI
   `unwinding` caps green → yellow. OI `building` is a bonus (lifts yellow → green
   when basis is opening). `gamma_fallback` or flow `red` → positioning = red.
2. **Cost block:** `cost.guard == block` → `Stand down` regardless of other signals.
   Cost `caution` does not block; it contributes to the `Mixed` path.
3. **Structural cap:** `structural == red` adds to `conflict_legs`; if positioning
   has a side (green or yellow), this triggers `signal_conflict`.
4. **Skew asymmetric oppose-veto:** `skew == oppose` adds to `conflict_legs` and
   triggers `signal_conflict`. `skew == agree` is subordinate corroboration — it
   never independently upgrades the verdict to `Favorable` and is never a "peer green".
   `skew == unavailable` is treated as neutral (no conflict, no bonus).
5. **Regime advisory (not a gate):** `regime == stand_down` is surfaced in `reasons`
   and contributes to the `Mixed` bucket; it does NOT hard-block independently (the
   operator's tile-surface brief decides how loudly to render it). Assert in tests
   that regime never produces a `calls`/`puts` direction output (CLAUDE.md, market-
   regime-header memory).
6. **Overall resolution:** `Stand down` when positioning = red OR cost = block.
   `Favorable` when positioning = green AND no `signal_conflict` AND cost != block
   AND skew != oppose. Everything else → `Mixed`.

**Unused signal visibility:**
- `signals_used` must list exactly the keys consumed in this funnel pass.
- `set(signals.keys()) - set(signals_used)` is the "computed but unused" set.
- A test asserts this set is empty when all expected signals are present (structural
  cannot be a silent strand).

**`unavailable` propagation:**
- A signal with `quality=unavailable` must not be treated as `green` or as a
  confirming value. The funnel must default to the conservative path for each
  unavailable input (skew → neutral; cost → caution; structural → yellow; OI → flat).

## Keepers to port from v2

- **`positioning_leg(direction_basis, flow_gate, oi_confirmation) -> str`** from
  `e1d6c5e:server/verdict.py`: the opening-flow/total-flow/unwinding/building logic is
  load-bearing; port verbatim, then refactor to consume `Signal` objects.
- **`skew_state(rr25, direction, *, thr) -> str`** from `e1d6c5e:server/verdict.py`:
  asymmetric oppose-veto is the key invariant; `agree` is subordinate. Port the logic;
  the threshold `_SKEW_THR = 0.02` is deferred to operator confirmation.
- **`compute_verdict(...)` funnel** from `e1d6c5e:server/verdict.py`: the cap/veto
  chain order, `conflict_legs` list, `signal_conflict` boolean, `overall` categories.
  The v3 port replaces the kwarg signature with `dict[str, Signal]` input; logic is
  unchanged until operator briefs say otherwise.
- **`_skew_expiry` date logic** from `e1d6c5e:server/snapshot.py`: 3rd-Friday monthly
  expiry >= ~25 DTE. This now belongs to Derive (it determines which SkewPoint to read),
  not Decide; confirm this migration in the derive spec.

**Dead v2 code to NOT port:**
- `flow_composite_score` percentile path (field was never set; see derive spec).
- `crush_risk` (always False because `trend` was always None; see derive spec).

## Acceptance criteria

- [ ] `decide({"flow": ..., "skew": ..., "cost": ..., "structural": ..., "positioning": ...})`
      returns a `Verdict` with `action` in `{Favorable, Mixed, Stand down}` and
      `signals_used` equal to exactly the keys consumed.
- [ ] `set(signals.keys()) - set(verdict.signals_used)` is empty when all five core
      signals are present (no silent strand).
- [ ] `skew.quality=unavailable` → verdict does not degrade below what it would be
      without skew at all (neutral path, not a block).
- [ ] `skew=oppose` + `positioning=green` → `signal_conflict=True`, `overall=Mixed`,
      `"skew"` in `conflict_legs`.
- [ ] `skew=agree` alone (positioning=yellow, structural=yellow, cost=ok) →
      `overall=Mixed`, not `Favorable` (agree is never a peer green).
- [ ] `cost.guard=block` → `overall="Stand down"` regardless of other signal states.
- [ ] `regime.direction` output is asserted absent (regime is never `calls`/`puts`).
- [ ] No `datetime.now()`, no `import requests`, no `import storage` in `decide.py`
      (CI purity lint asserts this, same pattern as Derive).
- [ ] `Verdict.provenance` is `Provenance.worst(*consumed_signal_provenances)`.

## Definition of done (universal)

Typed in/out (`dict[str, Signal]` in; `Verdict` out) · provenance: `Verdict.provenance`
is `worst()` of all consumed signals · no boundary skipped (no raw data or canonical
records accessed directly) · REPLAY-reproducible: same signal map → identical `Verdict`
(pure function).

## Defers to operator

- Exact `action` string copy (`"Worth acting on — the rare one"` etc.) — operator-
  held surface brief; the structural outcome categories (`Favorable/Mixed/Stand down`)
  are fixed.
- `_SKEW_THR` value (currently `0.02` in v2) — confirmed by Phase-2 empirical RR
  magnitude findings before locking.
- `_DELTA_TOL` for derived RR25 (currently `0.10` in v2's `derive_rr25`) — deferred
  to operator's skew-positioning brief.
- Whether `regime=stand_down` becomes a hard gate or remains advisory — deferred.

## Open questions / flags

- In v3, `positioning` is computed in Derive (a signal), not re-computed inside Decide
  from raw `direction_basis` + `flow_gate` + `oi_confirmation` kwargs. Confirm the
  Derive spec emits `Positioning{value: green|yellow|red, provenance}` so that Decide
  simply reads `signals["positioning"].value`. If Decide still needs `direction_basis`
  to enforce the opening-flow rule, that field must be on the `Flow` signal, not
  re-derived here.
- `signal_conflict` and `conflict_legs` should remain on `Verdict` (not moved to
  Present) because Present's tone logic depends on them for the `tone` field on
  conflicted elements.
