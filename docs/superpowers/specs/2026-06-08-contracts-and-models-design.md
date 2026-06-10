# Contracts & Domain Models — Design (v3, Phase 0)

> **AS-BUILT (2026-06-10).** `server/models/__init__.py` is the truth. Deltas:
> - `Element` grew `meaning` (terse number readout), `logic` (the rule that turns data
>   into the word), and `series` ({kind, points[]} chart data for the future UI). `tone`
>   vocabulary is `positive|cautionary|negative|neutral|unavailable`.
> - `ViewModel` grew `regime` (the market-context Element, data not posture) and
>   `verdict_logic` (how the call is reached).
> - **There is no Regime Signal** (deleted; market context is raw parts assembled in the
>   orchestrator). Domain entities: Flow, Conviction, Positioning, DealerGamma, Skew, Cost.
> - Canonicals beyond FlowAlert (all golden-tested): GreekFlowPoint, GammaStrike,
>   SkewPoint, IVTermPoint, OISnapshot, OptionContract (carries the OCC `symbol`),
>   TermStructurePoint, GreeksRow, ContractOIBar (root key `chains`).
>
> **CROSS-STAGE INVARIANTS** (added after review — the bugs lived BETWEEN stages):
> 1. **One side-picker.** `derive.flow_side(alerts)` is the ONLY function that picks the
>    call/put side; verdict direction, the cost contract, and the OI cluster all consume
>    it. A second picker re-creates the verdict/cost split bug.
> 2. **One session window.** `derive.session_alerts` is the ONLY flow-window filter;
>    every flow consumer (direction, cluster, grid) reads through it.
> 3. **Fetch params must match the golden capture's params.** A fixture captured with
>    `expiry=` pins THAT series; fetching without it may 200 a DIFFERENT series
>    (live-caught: the no-expiry RR series differs 7×). If orchestrator params diverge
>    from `tests/fixtures/bronze/*/SPY._meta.json`, re-capture or justify.
> 4. **Band coverage is part of the gamma contract**: derive refuses a one-sided strike
>    band rather than computing a sign from it.

**Status:** AS-BUILT (was PLAN) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** nothing (gates every other component) · **Starting point:** existing `server/models/__init__.py`

## Purpose
Define every **typed boundary** in the pipeline once, so each stage targets a declared
contract and consumes only the previous stage's typed output. This is the shared
vocabulary; no stage invents its own shapes. No I/O lives here.

## The five boundary contracts (one per stage edge)

```
ingest    →  RawRecord            (verbatim UW payload + metadata)
normalize →  list[Canonical]      (validated pydantic per endpoint: FlowAlert, OISnapshot, …)
derive    →  dict[str, Signal]    (name → Signal; pure functions produce these)
decide    →  Verdict              (built in one place; consumes signals by name)
present   →  ViewModel            (Element[]; the frontend renders this, computes nothing)
```

Rule: a stage's function signature references the contract type on both sides. A stage
may not import another stage's internals — only the contract models.

## Provenance — a type on every value (already in models)
`Provenance{source: live|cache|archive|derived, quality: real|degraded|unavailable,
as_of, note}` with `Provenance.worst(*p)` for derived merges. Every value-bearing model
carries a `provenance`. This subsumes v2's `is_synthetic` / honest-degrade / freshness
(port the worst-case logic from `e1d6c5e:server/freshness.py`).

## Models to define / extend

### Ingest contract
- `RawRecord{endpoint, params, ticker?, fetched_at, content_hash, payload, from_replay}`
  (exists in `server/pipeline/ingest.py`; promote the dataclass shape into models if a
  stage other than normalize needs it — otherwise leave at the ingest boundary).

### Canonical records (Normalize output) — typed, validated, the ONLY thing derive reads
Define per endpoint as needed by signals. Phase 3 needs **`FlowAlert`** at minimum:
- `FlowAlert{ticker, type: call|put, strike, expiry, total_premium, volume_oi_ratio,
  created_at, has_singleleg, has_multileg, total_ask_side_prem, total_bid_side_prem,
  has_sweep, provenance}` — field names mirror v2's validated shape (`e1d6c5e:server/schema.py`,
  `uw.flow_records`). Validation (sign/keys/required) happens in normalize, asserted
  against golden bronze.
- Later (Phase 4): `OISnapshot`, `GreekFlowSeries`, `GammaExposure`, `SkewPoint`,
  `IVTermPoint`, `EarningsEvent`, etc. — declared as their signals land, each with a
  golden fixture. Field shapes are **resolved empirically in Phase 2**, not assumed.

### Signals (Derive output) — first-class entities (exist as stubs)
`Signal` base (value + `provenance`, no I/O) and the entities: `Flow`, `Positioning`,
`DealerGamma`, `Skew`, `Cost`, `Regime`. Concrete fields fill in per derive spec; the
**direction** read on `Flow` is the Phase-3 first field. Each signal can be
`quality=unavailable` (no fabricated value) rather than absent.

### Verdict (Decide output) — exists
`Verdict{action, reasons[], signals_used[], provenance}`. `signals_used` makes a
computed-but-unused signal a visible gap.

### View model (Present output) — exists
`Element{key, label, surface, detail, provenance, tone}` + `ViewModel{ticker, as_of,
elements[], verdict}`. `surface` = the glance; `detail` = the tap payload. The frontend
renders these verbatim.

## Acceptance criteria
- [ ] Every stage edge has a named model; stage signatures use them; no cross-stage
      internal imports (enforceable by a simple import-lint in Phase 6).
- [ ] `Provenance.worst()` unit-tested: worst quality + worst source + oldest as_of win.
- [ ] `FlowAlert` validates a real flow-alerts bronze row (Phase 2 fixture) and rejects
      a malformed one.
- [ ] All models are pydantic v2, importable with no side effects, no I/O.

## Definition of done (universal — from the plan)
Typed in/out; provenance on every value; no boundary skipped; offline-reproducible.

## Defers to operator
Concrete signal fields/units and which canonical records exist beyond `FlowAlert` are
driven by the signal specs + the deferred research briefs — declared as each signal lands,
not up front.

## Open questions / flags
- Keep `RawRecord` at the ingest boundary (dataclass) vs promote to a pydantic model in
  `models`? Recommend: promote only if a non-normalize consumer appears.
- Canonical field names should be pinned to the Phase-2 golden bronze, not to v2's
  assumptions — v2's `schema.py` is a strong prior, not gospel.
