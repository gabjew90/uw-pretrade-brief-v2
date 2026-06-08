# v3 Build Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking. **Do not implement past Phase 0
> until the operator approves this plan and the per-component specs.**

**Goal:** Rebuild the pretrade brief on the v3 architecture — a typed 5-stage pipeline
over cross-cutting services — by first proving ONE signal end-to-end (a walking
skeleton), then filling breadth, with the frontend rendering a server-built view model
and computing nothing.

**Architecture:** Locked in `CLAUDE.md` + `docs/architecture.md` (the constitution).
5 stages — ingest → normalize → derive → decide → present — with a typed pydantic
contract at every boundary; cross-cutting clock / provenance / governor / storage;
append-only bronze/silver/gold parquet read via DuckDB; one FastAPI process.

**Tech stack:** Python 3.11 · FastAPI + uvicorn · pydantic v2 · requests (UW, 429
backoff) · pyarrow (parquet write) · duckdb (read) · google-genai (optional) ·
pytest + pytest-mock + responses + pytest-asyncio. Deploy = Dockerfile + railway.toml
(already restored, deployed green).

---

## Constitution conformance (non-negotiable anchors)

This plan conforms to and does not re-litigate: the 5-stage pipeline with a typed
contract at every boundary; the cross-cutting services; **"the frontend computes
nothing"**; the locked stack; and the "deliberately NOT doing" list (no microservices/
streaming/k8s/HA/warehouse). Deviations are flagged to the operator in **§Operator
flags**, never made silently.

## Planning principles (how this plan is ordered)

1. **Walking skeleton first** — build *direction-from-opening-flow* end-to-end through
   all five stages before any breadth. It's the product's core.
2. **Contracts before stages** — Phase 0 defines every typed boundary model; each stage
   targets a declared contract and consumes only the previous stage's typed output.
3. **Golden bronze before derive** — capture real UW payloads and resolve the data
   unknowns empirically (Phase 2) before any signal math is trusted.
4. **Foundations before pipeline** — clock, provenance, storage, governor, UW client
   (Phase 1) come before the stages that use them. `clock.py` already has tests —
   implement to pass and expand them first.
5. **Frontend last, and dumb** — renders the view model, computes nothing; a CI check
   fails the build on client-side signal logic.
6. **Ops/CI threaded throughout**, not bolted on at the end.

---

## Phase order & dependencies

```
Phase 0  Contracts & domain models ............ (no deps; gates everything)
Phase 1  Cross-cutting services ............... (deps: 0)        [clock has tests already]
Phase 2  Golden bronze + resolve data unknowns  (deps: 1 uw_client+storage) [BLOCKS 3]
Phase 3  Walking skeleton: direction E2E ...... (deps: 0,1,2)    [proves the boundaries]
Phase 4  Remaining signals into derive ........ (deps: 3)        [breadth]
Phase 5  Frontend as view-model renderer ...... (deps: 3 view model exists)
Phase 6  Ops/CI ............................... (threaded; finalized here)
```

Each phase below has a dedicated **design spec** (see §Spec index) that the operator
reviews alongside this plan. Acceptance criteria here are the phase-level gates; the
specs hold the per-component detail.

---

### Phase 0 — Contracts & domain models
**Spec:** `2026-06-08-contracts-and-models-design.md`
The boundary pydantic models (RawRecord, canonical records, Signal, Verdict,
Element/ViewModel) and the domain entities (Flow, Positioning, DealerGamma, Skew, Cost,
Regime, Verdict). No I/O. The existing `server/models/__init__.py` is the starting
point — extend it.

**Acceptance:**
- [ ] Every cross-stage boundary has a named pydantic model; each stage's signature
      references them (ingest→RawRecord, normalize→canonical, derive→Signal map,
      decide→Verdict, present→ViewModel).
- [ ] `Provenance` (source/quality/as_of) is a field on every value-bearing model;
      `Provenance.worst()` merges correctly (unit-tested).
- [ ] `mypy`/pydantic validates; no stage imports another stage's internals.
- **Checkpoint:** operator confirms the contracts before any stage is built.

### Phase 1 — Cross-cutting services
**Specs:** `clock`, `provenance`, `storage`, `governor`, `uw-client` (5 specs).
Implement: clock (sessions/holidays/half-days/phase + OI settlement cadence — tests
exist, expand); provenance helpers; storage (append-only parquet temp→`os.replace`,
DuckDB read, bronze/silver/gold); governor (priority + coalescing + budget meter, prefers
UW headers); UW client (hyphenated paths + 429 backoff, governor-gated).

**Acceptance:**
- [ ] `tests/test_clock.py` passes and is expanded (half-days, settlement edges).
- [ ] storage round-trips a parquet part (write→DuckDB read) in a tmp dir; writes are
      atomic + append-only (no read-modify-write anywhere).
- [ ] governor denies live calls in REPLAY and over-cap; records per-call; coalesces
      identical in-flight requests (unit-tested with a fake clock/UW).
- [ ] UW client `assert_hyphenated()` rejects underscore paths; 429 backoff unit-tested
      with `responses`.
- **Checkpoint:** services green in isolation before the pipeline consumes them.

### Phase 2 — Golden bronze + resolve data unknowns  **(BLOCKS Phase 3)**
**Spec:** `2026-06-08-golden-bronze-and-data-unknowns-design.md`
Capture real UW payloads into `tests/fixtures/bronze/` and resolve the unknowns in
§Data unknowns empirically. Uses the Railway bridge for live pulls; writes verbatim.

**Acceptance:**
- [ ] A golden bronze fixture exists for every endpoint Phase 3 needs (flow-alerts at
      minimum; greek-flow + net-prem-ticks + oi-per-strike for cross-checks).
- [ ] Each data unknown below has a written, evidenced answer committed into the spec
      (with the probe command + the observed result), not an assumption.
- [ ] A probe script (`scripts/probe_endpoints.py` re-homed) reports per-endpoint
      status + key presence + value sanity; exit≠0 on failure.
- **Checkpoint:** operator signs off the data findings before any signal math is trusted.

### Phase 3 — Walking skeleton: direction-from-opening-flow, end to end
**Spec:** `2026-06-08-derive-and-walking-skeleton-design.md`
One slice through all five stages: ingest(flow-alerts)→normalize(FlowAlert)→derive
(`direction` pure fn, golden-tested)→decide(trivial single-signal verdict)→present(view
model)→rendered by the dumb frontend.

**Acceptance:**
- [ ] `GET /api/view/<ticker>` returns a ViewModel whose direction element is computed
      by the pure `derive` fn from normalized flow, with provenance attached.
- [ ] Golden-fixture test asserts a **sane non-None direction value** (not just field
      presence) + a property test (opening-flow sign invariant).
- [ ] `REPLAY=1` reproduces the **identical** view model from captured bronze.
- [ ] The frontend renders it; **no client-side computation** of direction.
- **Checkpoint:** boundaries proven E2E — operator reviews before breadth.

### Phase 4 — Remaining signals into derive
**Spec:** extends the derive spec (Phase 3) per signal: conviction, dealer-gamma regime
(port GEX math), skew (25Δ RR), cost (IV/event/expected-move), positioning/confirmation
(OI trend from archive). Each a pure fn, golden-tested, wired into `decide` **by name**.

**Acceptance (per signal):**
- [ ] Pure (no I/O); time injected from clock.
- [ ] Golden-fixture value test + invariant test (e.g. GEX flip sign, greek-flow sign).
- [ ] Registered in derive; consumed by name in decide; an unused signal is a visible
      unused input (assert in a decide test).
- [ ] **Honest-degrade gated:** an `unavailable` value for this signal is named in the
      verdict's reasons; if it is a core signal, the verdict cannot be `Favorable`.
- [ ] **No same-family stacking:** the combiner obeys the locked decide-spec structure —
      concordant flow-family signals do not stack into a higher verdict; only divergence
      moves it (assert for conviction vs flow).
- **Checkpoint:** each signal reviewed as it lands (subagent-driven, two-stage review).

### Phase 5 — Frontend as a view-model renderer
**Spec:** `2026-06-08-frontend-design.md`
A component tree that renders the ViewModel; surface/tap progressive disclosure is a
**view-model property** (`element.surface` / `element.detail`), not render logic.

**Acceptance:**
- [ ] Renders every Element verbatim (surface, label, tap-detail, provenance tint).
- [ ] CI check (Phase 6) passes: no client-side signal/threshold/clock-for-decisions.
- [ ] Provenance quality renders consistently (real/degraded/unavailable).
- **Checkpoint:** operator reviews the rendered skeleton + the novice surface/tap.

### Phase 6 — Ops / CI
**Spec:** `2026-06-08-ops-ci-design.md`
Hyphenated-path lint; golden tests gating deploy; REPLAY=1 offline mode; the
no-client-computation CI check; **replay-as-backtest (re-derive N sessions of gold from
bronze → diffable signal history)**; nightly bronze backup to object storage; compaction
cron (separate from runtime).

**Acceptance:**
- [ ] CI runs golden tests + path lint + no-client-compute check; red blocks deploy.
- [ ] REPLAY=1 produces an identical view model offline in CI (determinism on one session).
- [ ] **Replay-as-backtest:** `scripts/backtest_replay.py` re-derives N (default 10)
      sessions of gold from captured bronze with **no live UW calls**, writes them to a
      `gold/backtest/<label>/` namespace (production gold untouched), and emits a
      line-diffable `signal_history.jsonl`. A re-run with a changed derive fn yields a
      readable diff confined to the affected signal. This is the payoff of immutable
      bronze + pure derive — distinct from the determinism check above.
- [ ] Backup + compaction documented and scheduled (cron, not in the request path).

---

## Data unknowns to resolve in Phase 2 (the invisible bugs)

Capture real payloads and assert against them — never assume field shapes. Each gets an
evidenced answer in the Phase 2 spec.

1. **greek-flow sign convention.** Validate on a clean, one-sided session that
   `sum(dir_delta_flow)` carries the expected sign, and pin it with an event check (a
   minute dominated by a known ask-side print carries the matching sign — e.g. the 6/5
   3:32pm ask-side put minute should be negative). Cross-check vs net-prem-ticks
   `net_delta`. **Never calibrate sign on a divergent session.**
2. **net-prem-ticks field population.** Confirm `net_call_premium`/`net_put_premium`/
   `net_delta` populate and the per-minute cumulative series is non-degenerate (v2 once
   saw `net_premium = 0`).
3. **flow-alerts truncation.** Find the record/time limit; determine whether a pulled
   "session" is the full day or only the last N alerts (v2 saw 118→394 between loads;
   cap 500, paginate via `older_than`).
4. **OI live-vs-settled + lookback depth.** Confirm what `oi-per-strike` returns intraday
   vs with `date=` and the settlement publish time, so the clock's OI cadence is correct.
   **(Confirmed in review: `oi-per-strike` IS `date=` backfillable — uw history
   `e1d6c5e:backfill.py`.)** Also **measure the actual lookback ceiling**: v2's tier probe
   observed 7 trading days (earliest 2026-05-13), which is account-age-dependent and may
   have grown — probe progressively older `date=` until it 404s/empties and set the
   governor's lookback bound to the measured value (see §Operator flags #3).
5. **Hyphenated paths return 200** for every endpoint used (underscore = silent 404).

---

## Definition of done — bake into every spec

- Typed contract in and out; no stage skips a boundary.
- Derive functions are pure; golden-fixture tests assert a **sane non-None value** (not
  just field presence), plus property/invariant tests (e.g. direction/GEX/greek-flow sign).
- Provenance (source / as_of / quality) attached to every value and rendered.
- **Honest-degrade is GATED as behavior, not just modeled as a type:** an `unavailable`
  signal is named in the verdict's `reasons`, and an unavailable *core* input (flow/
  positioning) makes `Favorable` impossible — never silently treated as neutral-green
  (decide spec; asserted by an inject-unavailable test).
- **Combination STRUCTURE is locked architecture, not deferred with the thresholds:** the
  signal families (Flow+OI+greek-flow conviction = one flow family; skew orthogonal) and
  the rule **"divergence vetoes; agreement never promotes"** are fixed in the decide spec.
  Only the numeric thresholds are operator-deferred — the combiner must not be rebuilt as
  a naive AND/OR that re-double-counts the flow family.
- Frontend renders the view model verbatim; a CI check rejects client-side computation.
- `REPLAY=1` reproduces the identical view model offline from captured bronze; the
  replay-as-backtest script re-derives history into a separate gold namespace.

## What this plan DEFERS (product specifics, not architecture)

Gate thresholds, verdict-funnel tuning, signal parameters, and the novice-readability
copy / surface-tap layout are **operator-supplied**, not part of this plan. They arrive
from v2's research keepers (in history at `e1d6c5e`) and the operator's existing briefs
(`2026-06-07-tile1-novice-readability-design.md`, `…-tile2-…` — operator-held, not in
repo). The plan builds the boundaries + the walking skeleton; signal math and UX copy
fill into the derive/present specs **after approval**. Do not invent thresholds or
alpha claims.

## Keepers — re-home, don't reinvent (in v2 history at `e1d6c5e`)

Port the correct v2 logic into the new boundaries via `git show e1d6c5e:server/<f>.py`:
- **GEX math** → `e1d6c5e:server/gex.py` (flip = nearest cumulative-net crossing; walls
  call/put separate; `gex_sign`) → derive `dealer_gamma`.
- **Greek-flow delta** → `e1d6c5e:server/greek_flow.py` (per-minute sum/cumsum; sign
  caveat) → derive `conviction`.
- **Skew + verdict legs** → `e1d6c5e:server/verdict.py` (25Δ RR sign-correction;
  asymmetric oppose-veto; funnel) → derive `skew` + decide.
- **Gates** → `e1d6c5e:server/gates.py` (direction-from-opening-flow; the 4 gates) →
  derive + decide.
- **Market regime** → `e1d6c5e:server/market_regime.py` (NEVER a direction call) →
  derive `regime`.
- **Budget meter** → `e1d6c5e:server/budget.py` → governor.
- **Degrade/freshness** → `e1d6c5e:server/freshness.py` → the provenance type.
- **Research-grounded signal definitions + thresholds** → the design briefs in
  `e1d6c5e:docs/superpowers/specs/` (signal-honesty, skew-positioning, greek-flow-delta,
  market-regime-header) — these supply the deferred math/thresholds post-approval.

## Operator flags (things in/around the constitution to confirm — not silent deviations)

1. **Rate-limit/lookback numbers are doc-claims, not code-proven.** v2 docstrings say
   "120/min, ~15k/day, 30-day lookback"; the budget meter actually trusts UW response
   headers. The governor should treat headers as truth and these as fallbacks. Confirm.
2. **net-prem-ticks may be redundant.** v2 found `net-prem-ticks.net_delta` ==
   `greek-flow.dir_delta_flow` (same field). Phase 2 should decide whether to ingest
   net-prem-ticks at all, or use it only as the greek-flow sign cross-check.
3. **The "7-day ceiling" in the governor scaffold WAS observed in v2, and is
   account-age-dependent.** v2's tier probe saw it directly ("earliest date available to
   you is 2026-05-13, 7 trading days"), so it is not invented — but it is a property of
   the account's history depth at probe time, which may have grown since. Action: **re-verify
   the actual lookback depth in Phase 2** (probe `oi-per-strike`/history with progressively
   older `date=` until it 404s/empties) and set the governor's ceiling to the *measured*
   value, rather than treating 7 as fixed or dropping the bound entirely.
4. **Cleanup:** the standalone `uw-pretrade-brief-v3` dir + GitHub repo are now
   redundant (operator to delete).

## Spec index (review these alongside this plan)

- `docs/superpowers/specs/2026-06-08-contracts-and-models-design.md` (Phase 0)
- `…-clock-design.md`, `…-provenance-design.md`, `…-storage-design.md`,
  `…-governor-design.md`, `…-uw-client-design.md` (Phase 1)
- `…-golden-bronze-and-data-unknowns-design.md` (Phase 2)
- `…-derive-and-walking-skeleton-design.md` (Phase 3/4), `…-ingest-design.md`,
  `…-normalize-design.md`, `…-decide-design.md`, `…-present-and-viewmodel-design.md`
- `…-frontend-design.md` (Phase 5)
- `…-ops-ci-design.md` (Phase 6)

---

## Execution handoff (after approval)

Recommended: **superpowers:subagent-driven-development** — fresh subagent per task,
two-stage review (spec compliance, then code quality), in dependency order 0→6. Each
phase ships independently and redeploys via the existing Dockerfile/railway.toml.
**Until approved: stop after Phase 0 scaffolding (already in place).**
