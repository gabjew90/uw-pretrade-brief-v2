# Normalize — Design (v3, Phase 2)

**Status:** PLAN (awaiting approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** Phase 0 (contracts — `FlowAlert`, `Provenance`, canonical model stubs); Phase 1 (Ingest — `RawRecord`)
**Starting point:** `server/pipeline/normalize.py` (registry + dispatch scaffold exists)

## Purpose / role

Parse a `RawRecord` (bronze) into a validated list of canonical pydantic models. This
is the **single "detect, don't trust" boundary** for the entire pipeline. Every field-
shape assumption about the UW API is stated and tested here — hyphenated-path results,
truncation flags, sign conventions, key drift, and missing required fields all become
explicit failures at this boundary, never silent nulls three layers downstream. Nothing
downstream reads bronze; nothing upstream validates field shapes.

## Contract (typed in/out)

```
normalize(raw: RawRecord) -> list[CanonicalModel]
```

- **Input:** `RawRecord` from Ingest (verbatim payload + metadata).
- **Output:** a non-empty list of validated pydantic models for the endpoint (e.g.
  `list[FlowAlert]`, `list[OISnapshot]`, `list[GreekFlowSeries]`). An empty `[]` means
  "no records in a structurally valid response" (e.g. pre-market, no alerts yet) — that
  is legitimate. Contrast with a parse error (wrong shape), which raises.
- **Error contract:** raises `NormalizeError` (a typed exception, not a broad `Exception`)
  when the payload shape is irrecoverably wrong — missing required keys, wrong type on a
  required field, sign convention violation. The caller decides whether to degrade
  gracefully or propagate.
- **Dispatch:** `REGISTRY[endpoint_key] -> parser(raw) -> list` — one registered parser
  per endpoint. A missing registration is a `NotImplementedError` (wiring error, always
  loud).

## Responsibilities

**Normalize owns:**
- Dispatching `raw.endpoint` to the correct registered parser.
- ALL field-shape validation: required fields present, types correct (e.g.
  `volume_oi_ratio` is numeric, not a string `"N/A"`), enum values in range, sign
  conventions match the canonical model's documented convention.
- Constructing `Provenance` on each canonical model from `raw.fetched_at` and
  `raw.from_replay` (source = `live` vs `archive`; quality = `real` vs `degraded`
  vs `unavailable` based on whether required fields are present).
- Flagging truncation explicitly: flow-alerts truncation (when `len(alerts) == page_cap`,
  the session tail may be missing — set a `truncated: bool` flag on the canonical record
  or its container, never silently pass it through).
- Sign-correcting vendor fields that arrive in a non-standard convention, at this
  boundary, with a comment stating the correction and the empirical pin (see
  `e1d6c5e:server/verdict.py::extract_vendor_rr` — vendor `risk_reversal` = put−call,
  negated here to call−put convention).

**Normalize does NOT own:**
- Reading bronze storage directly (receives `RawRecord` from Ingest).
- Any network I/O or governor calls.
- Signal math or derived values (Derive's job).
- Deciding which endpoints to normalize or in what order (orchestrator).
- Persisting silver records (storage service's job, called by the orchestrator after
  normalize returns).

## Key behaviors / edge cases

- **`detect, don't trust` (CLAUDE.md):** UW failures degrade silently at the HTTP layer
  (a 200 with `{}` or a missing key looks like "no data"). Normalize must assert that
  required keys are present and raise `NormalizeError` on violation — never produce a
  canonical model with a fabricated default for a required field.
- **Golden fixtures, not hand-written:** canonical model validation must be tested against
  REAL captured bronze rows (Phase 2 pull). A hand-written fixture enshrines the wrong
  assumption. Assert that the parser returns a sane non-None *value*, not just that a key
  exists (the iv/rv key bug in v2 was caught only by asserting the value).
- **flow-alerts truncation flag:** `len(rows) == page_cap` is not a hard truncation proof
  (the feed could coincidentally fill the cap), but it is the only signal available.
  Emit `truncated=True` and let Tile 1 / Derive surface this honestly (see
  `e1d6c5e:server/snapshot.py` flow-alerts-truncation note and memory).
- **Per-minute Greek flow fields:** `total_delta_flow` and `dir_delta_flow` are
  cumulative-per-minute series (sum/cumsum, never a last-tick scalar). Validate they are
  lists, not scalars; fail loudly if the shape changes.
- **sign pinning:** `historical-risk-reversal-skew` → vendor RR = put−call (positive =
  put skew). After sign correction at this boundary the canonical `SkewPoint.rr` is
  call−put (positive = call skew). The correction is stated in a doc comment pinned to
  the empirical cross-check in `e1d6c5e:server/verdict.py::extract_vendor_rr`.
- **OI settlement cadence:** `OISnapshot` rows include a `provisional` flag: OI at the
  current session has not yet settled (settles ~9am next business morning). Clock service
  provides the session phase; Normalize stamps `provisional=True` on today's OI rows.
  (Port logic from `e1d6c5e:server/schema.py::OISessionBar.provisional`.)
- **Endpoint key collision:** `_ep_key` must produce the same slug Ingest uses for the
  bronze partition key (strip `/`, replace `/` with `_`). Any divergence causes a miss.

## Keepers to port from v2

- **`FlowAlert` field names** from `e1d6c5e:server/schema.py` and `e1d6c5e:server/uw.py`
  (`uw.flow_records`): `ticker, type, strike, expiry, total_premium, volume_oi_ratio,
  created_at, has_singleleg, has_multileg, total_ask_side_prem, total_bid_side_prem,
  has_sweep`. These are the live-confirmed field names; pin them to Phase-2 golden
  bronze before assuming they are stable.
- **`extract_vendor_rr` sign correction** from `e1d6c5e:server/verdict.py`: vendor
  `risk_reversal` is negated; the pin comment ("vendor +0.051 ≈ −derived −0.060 for SPY")
  must travel with the parser.
- **`_extract_ivr` / `_extract_iv_curve` key candidates** from
  `e1d6c5e:server/snapshot.py`: iv/rv key bugs were the canonical invisible failure; the
  Phase-2 golden fixture must assert the actual key names (`implied_volatility`,
  `realized_volatility`) not any assumed variant.
- **`OISessionBar.provisional`** flag logic from `e1d6c5e:server/schema.py` — today's
  OI is live, not settled; the flag prevents the positioning gate from treating
  provisional OI as confirmed.
- **Truncation flag** from the flow-alerts-truncation memory entry: `len == cap` heuristic;
  `Tile1.truncated` in the ViewModel downstream.
- **`_regime_vol(payload)`** key handling from `e1d6c5e:server/snapshot.py`: `rows[-1]`
  for `implied_volatility`; walk backward for latest non-None `realized_volatility`. Port
  as the `VolatilityRecord` normalizer — do not silently return `(None, None)` on key
  miss; raise `NormalizeError` on completely unexpected shape.

## Acceptance criteria

- [ ] `normalize(raw)` where `raw` is a real flow-alerts bronze row (Phase-2 fixture)
      returns `list[FlowAlert]` with sane non-None values for `ticker`, `type`, `strike`,
      `total_premium`, `volume_oi_ratio`; not just "fields present".
- [ ] A malformed flow-alerts row (missing `volume_oi_ratio`) raises `NormalizeError`,
      not a pydantic `ValidationError` that leaks to the caller untyped.
- [ ] `truncated=True` is set on the container when `len(alerts) == page_cap`.
- [ ] Vendor RR bronze row parses with the sign-corrected `rr` value matching the
      empirical pin (port the SPY cross-check as a golden test).
- [ ] `OISnapshot` rows for today's session have `provisional=True`; yesterday's rows
      have `provisional=False`.
- [ ] A parser for an unregistered endpoint raises `NotImplementedError` (not a silent
      empty list).
- [ ] All canonical models are importable with no side effects; no I/O in any parser.
- [ ] CI: `tests/test_uw_paths.py` endpoint-slug lint passes (normalize uses the same
      `_ep_key` as Ingest).

## Definition of done (universal)

Typed in/out (`RawRecord` in; `list[CanonicalModel]` out) · provenance: every canonical
model carries `Provenance{source, as_of, quality}` constructed from `raw.fetched_at` +
`raw.from_replay` · no boundary skipped (nothing reads bronze except normalize) ·
REPLAY-reproducible: same `RawRecord` → identical canonical list on every run (parsers
are pure given the same input).

## Defers to operator

- Which endpoints beyond `flow-alerts` have parsers in Phase 3 vs Phase 4 (determined
  per signal landing order in the derive spec).
- Thresholds for "sane value" in golden tests (e.g. IV range 0.05–5.0, RR magnitude
  <0.3) — filled from Phase-2 empirical findings, not invented here.
- Whether `NormalizeError` should carry a structured `{endpoint, field, reason}` payload
  for observability (nice-to-have; the raise is required, the shape is deferred).

## Open questions / flags

- Should silver persistence (writing canonical records to silver parquet) be triggered
  inside `normalize` or by the orchestrator after `normalize` returns? Recommendation:
  orchestrator — keeps normalize pure (no I/O) and lets the orchestrator decide whether
  to persist (e.g. skip persistence during a short REPLAY test run).
- `GreekFlowSeries` cumsum shape: confirm whether the Phase-2 bronze shows a list of
  `{minute, cumulative_delta}` dicts or a flat running total at the endpoint level.
  The v2 `greek_flow_mod.build_composite` input was a raw dict — pin the actual shape
  before writing the parser (the sign was not self-validating in v2; see derive spec).
