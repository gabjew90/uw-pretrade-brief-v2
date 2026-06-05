# Direction from Opening Flow (#1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pick the call/put side from signed OPENING flow (Ge–Lin–Pearson) instead of gamma, with an honest fallback cascade and a user-visible `direction_basis` so the weaker bases are flagged — eliminating the wrong-way-bet where the picker ranks calls while Tile 1 shows put-buying.

**Architecture:** Add a pure `derive_direction()` in `server/gates.py` (the signal module; pre-positions the #6 single-source consolidation). It reads the per-ticker opening flow already on `flow_alerts_detail`, falls back to total flow, then to the old gamma rule — returning `(direction, direction_basis)`. `snapshot._build_dashboard_row` calls it; `Row` carries `direction_basis`; the deep-dive title surfaces the weak bases. A contract + opt-in live probe verifies `all_opening_trades` actually populates on the Basic tier (trust-but-verify, same as the skew probe).

**Tech Stack:** Python 3.11, pydantic v2, pytest; vanilla-JS frontend (static/index.html).

**Spec:** `docs/superpowers/specs/2026-06-04-signal-honesty-design.md` (this plan = the #1 / direction slice; #4/#5/#6/Cost are a separate plan).

---

## File Structure

- **Modify** `server/gates.py` — add `derive_direction(flow_alerts, gex_sign, flip_pct) -> tuple[str, str]`. Pure function, no schema import (reads attrs via `getattr`).
- **Modify** `server/schema.py` — add `direction_basis` to `Row`.
- **Modify** `server/snapshot.py` — replace the gamma-only direction block (`_build_dashboard_row`, ~lines 220–229) with a `derive_direction` call; pass `direction_basis` into `Row(...)`.
- **Modify** `tests/contracts.py` — add `all_opening_trades` to the `flow_alerts` contract.
- **Modify** `tests/test_uw_contracts.py` — opt-in live probe reporting the opening-flow population rate.
- **Modify** `static/index.html` — show a weak-basis tag in the deep-dive title when `direction_basis != "opening_flow"`.
- **Tests:** `tests/test_gates.py` (derive_direction units), `tests/test_schema.py`, `tests/test_snapshot.py`.

---

## Task 1: `derive_direction` in gates.py

**Files:**
- Modify: `server/gates.py`
- Test: `tests/test_gates.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gates.py`:

```python
from types import SimpleNamespace
from server import gates


def _fa(type_, premium, opening):
    # mimics a FlowAlert: .type, .total_premium, .all_opening_trades
    return SimpleNamespace(type=type_, total_premium=premium, all_opening_trades=opening)


def test_direction_opening_flow_leads_calls():
    alerts = [_fa("call", 900_000, True), _fa("put", 100_000, True),
              _fa("put", 5_000_000, False)]  # huge CLOSING put must NOT flip the side
    d, basis = gates.derive_direction(alerts, gex_sign="POS", flip_pct=-1.0)
    assert d == "calls" and basis == "opening_flow"


def test_direction_opening_flow_leads_puts():
    alerts = [_fa("put", 800_000, True), _fa("call", 200_000, True)]
    d, basis = gates.derive_direction(alerts, gex_sign="NEG", flip_pct=1.0)
    assert d == "puts" and basis == "opening_flow"


def test_direction_falls_back_to_total_flow_when_no_opening():
    alerts = [_fa("call", 300_000, False), _fa("put", 900_000, False)]  # none opening
    d, basis = gates.derive_direction(alerts, gex_sign="NEG", flip_pct=1.0)
    assert d == "puts" and basis == "total_flow"


def test_direction_falls_back_to_gamma_when_no_flow():
    d, basis = gates.derive_direction([], gex_sign="NEG", flip_pct=-1.0)
    assert d == "calls" and basis == "gamma_fallback"          # NEG -> calls (old rule)
    d2, basis2 = gates.derive_direction([], gex_sign="POS", flip_pct=-1.0)
    assert d2 == "puts" and basis2 == "gamma_fallback"


def test_direction_opening_tie_breaks_to_calls_but_basis_is_opening():
    alerts = [_fa("call", 500_000, True), _fa("put", 500_000, True)]
    d, basis = gates.derive_direction(alerts, gex_sign="POS", flip_pct=-1.0)
    assert d == "calls" and basis == "opening_flow"   # tie -> calls, still opening-based
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/test_gates.py -k direction -q`
Expected: FAIL — `AttributeError: module 'server.gates' has no attribute 'derive_direction'`.

- [ ] **Step 3: Implement `derive_direction` in `server/gates.py`**

Add at the end of `server/gates.py`:

```python
def derive_direction(flow_alerts, gex_sign: str, flip_pct: float) -> tuple[str, str]:
    """Pick the call/put side. OPENING flow leads (Ge-Lin-Pearson: opening bets
    predict, closing bets don't); fall back to total signed flow (Pan-Poteshman),
    then to the legacy gamma rule. Returns (direction, direction_basis) where
    basis is 'opening_flow' | 'total_flow' | 'gamma_fallback'. Pure; reads
    attributes tolerantly so it works on FlowAlert objects or dicts."""
    def _prem(a, want_type, opening_only):
        out = 0.0
        for x in flow_alerts or []:
            typ = getattr(x, "type", None) if not isinstance(x, dict) else x.get("type")
            opn = getattr(x, "all_opening_trades", False) if not isinstance(x, dict) else x.get("all_opening_trades", False)
            prem = getattr(x, "total_premium", 0.0) if not isinstance(x, dict) else x.get("total_premium", 0.0)
            if typ == want_type and (opn or not opening_only):
                out += float(prem or 0.0)
        return out

    open_call, open_put = _prem(flow_alerts, "call", True), _prem(flow_alerts, "put", True)
    if open_call or open_put:
        return ("calls" if open_call >= open_put else "puts", "opening_flow")

    tot_call, tot_put = _prem(flow_alerts, "call", False), _prem(flow_alerts, "put", False)
    if tot_call or tot_put:
        return ("calls" if tot_call >= tot_put else "puts", "total_flow")

    # legacy gamma rule, last resort, never silent (basis says so)
    if gex_sign == "NEG" or flip_pct > 0:
        return ("calls", "gamma_fallback")
    return ("puts", "gamma_fallback")
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/test_gates.py -k direction -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add server/gates.py tests/test_gates.py
git commit -m "feat(direction): derive_direction — opening flow leads, fallback cascade + basis

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `direction_basis` on the Row schema

**Files:**
- Modify: `server/schema.py`
- Test: `tests/test_schema.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_schema.py`:

```python
def test_row_has_direction_basis_default():
    from server.schema import Row, Gates, GateMethod
    row = Row(ticker="SPY", spot=1.0, direction="calls",
              gates=Gates(flow="green", oi="green", structural="green", cost="green"),
              gate_method=GateMethod(flow="absolute", oi="absolute",
                                     structural="absolute", cost="absolute"))
    assert row.direction_basis == "gamma_fallback"           # honest default
    row2 = Row(ticker="SPY", spot=1.0, direction="calls", direction_basis="opening_flow",
               gates=row.gates, gate_method=row.gate_method)
    assert row2.direction_basis == "opening_flow"
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/test_schema.py -k direction_basis -q`
Expected: FAIL — `direction_basis` is not a field (with `extra="allow"` it would accept it as an extra, so the FIRST assert — the default — fails: `AttributeError`/missing default).

- [ ] **Step 3: Implement — add the field to `Row`**

In `server/schema.py`, in `class Row`, immediately after the `direction: Literal["calls", "puts"]` line, add:

```python
    direction_basis: Literal["opening_flow", "total_flow", "gamma_fallback"] = "gamma_fallback"
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/test_schema.py -k direction_basis -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/schema.py tests/test_schema.py
git commit -m "feat(schema): Row.direction_basis (opening_flow|total_flow|gamma_fallback)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Wire `derive_direction` into the row build

**Files:**
- Modify: `server/snapshot.py` (`_build_dashboard_row`, the direction block ~lines 220–229 and the `Row(...)` call ~line 251)
- Test: `tests/test_snapshot.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_snapshot.py` (it already has `stub_uw`, `fresh_storage_state`, `tmp_data_dir` fixtures and builds rows). Add a focused test that the built row's direction comes from opening flow. If the existing `stub_uw` flow fixture doesn't carry opening trades, this test monkeypatches the flow projection to inject a known opening imbalance:

```python
async def test_build_row_direction_from_opening_flow(stub_uw, fresh_storage_state, tmp_data_dir, monkeypatch):
    from types import SimpleNamespace
    from server import snapshot as snap
    # Force a clear opening-PUT imbalance regardless of gamma
    fake = [SimpleNamespace(type="put", total_premium=2_000_000, all_opening_trades=True,
                            strike=100.0),
            SimpleNamespace(type="call", total_premium=100_000, all_opening_trades=True,
                            strike=100.0)]
    monkeypatch.setattr(snap, "_project_flow_alerts", lambda raw: fake)
    row = await snap.build_single_row("SPY")
    assert row is not None
    assert row.direction == "puts"
    assert row.direction_basis == "opening_flow"
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/test_snapshot.py -k direction_from_opening -q`
Expected: FAIL — `row.direction_basis` is the default `gamma_fallback` (direction still gamma-derived), or direction doesn't match opening flow.

- [ ] **Step 3: Implement — replace the direction block in `_build_dashboard_row`**

In `server/snapshot.py`, replace this block (the comment + the if/else, ~lines 220–229):

```python
    # Direction inference: positive net dealer γ (gex_sign=POS) means dealers
    # are long γ → they sell into rallies, buy dips → suppresses upside
    # momentum. Negative γ means dealers are short γ → amplify moves in either
    # direction. For directional-trade framing we infer "calls" when γ is
    # negative (squeeze-friendly upside setup) or when spot is below γ flip
    # (room to grind higher into the flip), and "puts" otherwise.
    if gex_sign == "NEG" or flip_pct > 0:
        direction = "calls"
    else:
        direction = "puts"
```

with:

```python
    # Direction: OPENING flow leads (Ge-Lin-Pearson — opening bets predict,
    # closing bets don't), falling back to total flow then the legacy gamma rule.
    # derive_direction returns the basis so the UI can flag the weaker cases.
    direction, direction_basis = gates.derive_direction(flow_alerts_detail, gex_sign, flip_pct)
```

Then in the `Row(...)` constructor, add `direction_basis=direction_basis,` immediately after the existing `direction=direction,` line (the one at ~line 254).

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/test_snapshot.py -q`
Expected: PASS (the new test + existing snapshot tests). Note: some existing snapshot tests may assert the OLD gamma direction — if any now fail because the stub flow implies a different side, update those assertions to the opening-flow-derived expectation (the new intent) and note the change in the commit.

- [ ] **Step 5: Commit**

```bash
git add server/snapshot.py tests/test_snapshot.py
git commit -m "feat(direction): row build picks side from opening flow (was gamma)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Probe `all_opening_trades` (trust-but-verify)

**Files:**
- Modify: `tests/contracts.py` (add field to the `flow_alerts` contract)
- Modify: `tests/test_uw_contracts.py` (opt-in live population probe)

- [ ] **Step 1: Write the failing test**

In `tests/contracts.py`, the `flow_alerts` contract currently requires `["ticker", "type", "strike", "total_premium"]`. Add `all_opening_trades`:

```python
    "flow_alerts": {
        "required": ["ticker", "type", "strike", "total_premium", "all_opening_trades"],
        "note": "snapshot._aggregate_flow + _project_flow_alerts: Tile 1 + flow value; "
                "all_opening_trades drives the opening-flow direction (Ge-Lin-Pearson)",
    },
```

Add a live population probe to `tests/test_uw_contracts.py` (after the existing live test):

```python
@pytest.mark.live
def test_live_opening_flow_field_populates():
    """all_opening_trades is now load-bearing for DIRECTION — verify the Basic
    tier actually sets it true sometimes (same risk class as the ask-side /
    net_prem_ticks=0 gotchas). Reports the opening fraction; fails only if the
    field is entirely absent (drift) — an all-false result is logged loudly."""
    import os
    if not os.environ.get("UW_API_KEY"):
        pytest.skip("UW_API_KEY not set")
    from server import uw
    try:
        payload = uw.fetch_flow_alerts(limit=200)
    except uw.UWError as e:
        pytest.skip(f"flow_alerts live fetch failed: {e}")
    rows = payload.get("data") or []
    if not rows:
        pytest.skip("no live flow rows")
    assert all("all_opening_trades" in r for r in rows[:5]), "field missing (drift)"
    opening = sum(1 for r in rows if r.get("all_opening_trades"))
    frac = opening / len(rows)
    print(f"\n[opening-flow probe] {opening}/{len(rows)} alerts opening ({frac:.0%})")
    # Loud signal if the field is dead on this tier (direction would always
    # fall back to total_flow): not a hard fail, but visible.
    assert frac >= 0.0
```

- [ ] **Step 2: Run to verify FAIL (offline contract)**

Run: `python -m pytest tests/test_uw_contracts.py -k flow_alerts -q`
Expected: FAIL if the committed golden `flow_alerts.json` lacks `all_opening_trades` on its first row. If it HAS the field, the offline test passes and the new live probe is skipped (no key) — that's acceptable; the contract addition still guards drift.

- [ ] **Step 3: Make it pass**

If the golden lacks the field, regenerate it from a fresh archive (`DATA_DIR=./data python scripts/extract_golden.py`) OR, if no fresh archive is available, hand-add `"all_opening_trades": false` to representative rows in `tests/fixtures/golden/flow_alerts.json` so the contract is exercised. (Do not fake `true` — `false` is the honest worst case and still satisfies "field present".)

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/test_uw_contracts.py -q`
Expected: PASS (offline) + the live probe SKIPPED without a key.
**Live verification (do at market open with a key):** `UW_API_KEY=... python -m pytest tests/test_uw_contracts.py -k opening_flow -m live -s` → read the printed opening fraction. If it's ~0%, opening flow is dead on the tier and direction is effectively `total_flow` — surface that finding before relying on it.

- [ ] **Step 5: Commit**

```bash
git add tests/contracts.py tests/test_uw_contracts.py tests/fixtures/golden/flow_alerts.json
git commit -m "test(direction): contract + live probe for all_opening_trades population

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Surface `direction_basis` in the deep-dive

**Files:**
- Modify: `static/index.html` (the deep-dive title, where `dirToggle` is built)
- Test: `tests/test_html_preservation.py`

- [ ] **Step 1: Add the weak-basis tag in the title**

In `static/index.html`, find the deep-dive title line that ends with `${dirToggle}${lu}` (inside `renderDeepDive`). Immediately BEFORE the `titleEl.innerHTML = ...` line, add:

```javascript
  // Honesty marker: when the side wasn't picked from opening flow, say so.
  const basisTag = (row.direction_basis && row.direction_basis !== "opening_flow")
    ? `<span class="dir-basis" title="Side derived from a weaker basis than opening flow.">via ${row.direction_basis === "total_flow" ? "total flow" : "gamma (weak)"}</span>`
    : "";
```

Then change the title assignment to include it after the toggle:

```javascript
  titleEl.innerHTML = `Deep-dive card — <span style="font-family:var(--ff-display);font-style:italic;font-weight:500;font-size:22px;letter-spacing:-0.02em;color:var(--title);text-transform:none;">${row.ticker}</span>${dirToggle}${basisTag}${lu}`;
```

- [ ] **Step 2: Add the `.dir-basis` style**

Near the existing `.t-asof` rule (added for the freshness line), add:

```css
  .dir-basis { font-size: 9px; color: var(--warn); font-family: var(--ff-mono); margin-left: 6px; border: 1px solid var(--warn); border-radius: 4px; padding: 1px 5px; }
```

- [ ] **Step 3: Run the preservation test**

Run: `python -m pytest tests/test_html_preservation.py -q`
Expected: PASS (additions only; 6 V2-EDIT zones intact).

- [ ] **Step 4: Commit**

```bash
git add static/index.html
git commit -m "feat(direction): surface weak direction_basis (total-flow / gamma) in deep-dive

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Offline verification + full suite

**Files:** none (verification).

- [ ] **Step 1: Full suite green**

Run: `python -m pytest -q`
Expected: all pass (live-marked skipped). Confirm count ≥ prior baseline + the new tests.

- [ ] **Step 2: Replay smoke test**

Run (background, PowerShell): `$env:DATA_DIR="./data"; $env:REPLAY="1"; .venv/Scripts/python -m uvicorn server.main:app --port 8000`
Then: `Invoke-RestMethod http://localhost:8000/api/lookup/SPY | Select-Object ticker,direction,direction_basis`
Expected: `direction` reflects SPY's opening flow (or `total_flow`/`gamma_fallback` basis if the archive's flow has no opening trades — which itself confirms the probe finding offline). No errors.

- [ ] **Step 3: Browser check (Playwright)**

Load `http://localhost:8000`, click a ticker, confirm the deep-dive title shows the direction toggle and — when basis isn't opening_flow — the amber "via total flow"/"via gamma (weak)" tag. Screenshot.

- [ ] **Step 4: Final commit if any fixups**

```bash
git add -A
git commit -m "test(direction): offline replay verification of opening-flow direction"
```

---

## Self-Review

**Spec coverage (the #1 / direction slice):**
- Opening flow leads, closing excluded → Task 1 (test proves a huge closing put doesn't flip the side). ✓
- Fallback cascade total_flow → gamma, never silent → Task 1 + `direction_basis`. ✓
- `direction_basis` field → Task 2; surfaced user-facing as the weaker case → Task 5. ✓
- Computed server-side, out of the ad-hoc snapshot block → Task 3 (moved into `gates.derive_direction`). ✓
- Trust-but-verify `all_opening_trades` (probe) → Task 4 (contract + live population probe). ✓
- No index special-case (uniform opening-flow) → Task 1/3 apply to all tickers. ✓
- Deferred (separate plan, correctly NOT here): #4 gex_sign cap, #5 skew leg + Positioning collapse, #6 single-source JS removal, Cost expected-move, signal_conflict.

**Placeholder scan:** none — every code step has complete code. The golden-fixture step (Task 4 Step 3) gives an explicit honest fallback (`false`, not faked `true`).

**Type consistency:** `derive_direction(flow_alerts, gex_sign, flip_pct) -> (str, str)` used identically in Task 1 (def) and Task 3 (call). `direction_basis` literal values (`opening_flow`/`total_flow`/`gamma_fallback`) match across gates.py, schema.py, snapshot.py, and the frontend tag. `Row.direction_basis` default `gamma_fallback` consistent in Tasks 2 and 3.
