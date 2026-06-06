# Skew Leg + Positioning Verdict (Plan 3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deep-dive-only `row.verdict` (Positioning + Structural + Skew legs, Cost guard, signal_conflict, overall, action) computed by a new pure `server/verdict.py`, with 25Δ risk-reversal **derived** from the `greeks` endpoint.

**Architecture:** A pure, heavily-tested `server/verdict.py` does all the logic (derive RR₂₅ from per-strike greeks, classify skew/positioning, assemble the verdict). `build_single_row` (the full click build) calls it, fetching ~30d greeks for the skew leg; light grid rows leave `verdict=None`. The deep-dive renders a 3-leg panel + action headline + conflict banner. The grid and `Gates` are untouched.

**Tech Stack:** Python 3.11, pydantic v2, pytest; vanilla JS in `static/index.html`.

**Spec:** `docs/superpowers/specs/2026-06-06-skew-positioning-verdict-design.md`

**Pre-verified facts (don't re-litigate):** `greeks` carries per-strike `call_delta`/`put_delta`/`call_volatility`/`put_volatility` populated at the 25Δ wings (probe 2026-06-06 + `tests/fixtures/golden/greeks.json`, 354 rows). `_structural_gate` already caps green→yellow on `gex_sign=="POS"` (gates.py:127-131). `Tile2.confirmation ∈ {building, flat, unwinding, unconfirmed}`. `derive_direction` returns basis ∈ {opening_flow, total_flow, gamma_fallback}.

---

### Task 1: `derive_rr25` (pure)

**Files:**
- Create: `server/verdict.py`
- Test: `tests/test_verdict.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_verdict.py
import json
from pathlib import Path
from server import verdict

_GREEKS = Path(__file__).parent / "fixtures" / "golden" / "greeks.json"


def test_derive_rr25_exact_from_synthetic_rows():
    rows = [
        {"call_delta": "0.50", "put_delta": "-0.50", "call_volatility": "0.30", "put_volatility": "0.40"},
        {"call_delta": "0.26", "put_delta": "-0.24", "call_volatility": "0.18", "put_volatility": "0.23"},  # nearest 25d
        {"call_delta": "0.10", "put_delta": "-0.90", "call_volatility": "0.15", "put_volatility": "0.50"},
    ]
    # RR25 = call_vol(0.26 leg) - put_vol(-0.24 leg) = 0.18 - 0.23 = -0.05
    assert abs(verdict.derive_rr25(rows) - (-0.05)) < 1e-9


def test_derive_rr25_none_when_no_strike_near_25d():
    rows = [{"call_delta": "0.95", "put_delta": "-0.95", "call_volatility": "0.2", "put_volatility": "0.2"}]
    assert verdict.derive_rr25(rows, tol=0.10) is None


def test_derive_rr25_runs_on_real_greeks_payload():
    rows = json.loads(_GREEKS.read_text(encoding="utf-8")).get("data")
    rr = verdict.derive_rr25(rows)
    assert rr is None or isinstance(rr, float)   # real-shape smoke: no crash on live columns
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_verdict.py -q`
Expected: FAIL — `server.verdict` does not exist.

- [ ] **Step 3: Implement**

```python
# server/verdict.py
"""Pure verdict synthesis for the deep-dive (Plan 3). NO I/O — callers pass
already-fetched data in. Derives 25-delta risk-reversal skew from greeks, then
assembles the 3-leg verdict (Positioning / Structural / Skew) + Cost guard +
signal_conflict + overall + action."""
from __future__ import annotations

_SKEW_THR = 0.02          # IV points: |RR25| below this is noise, not a signal
_DELTA_TOL = 0.10         # how far from ±0.25 a strike may be to count as the 25d leg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def derive_rr25(greeks_rows: list[dict], tol: float = _DELTA_TOL) -> float | None:
    """25-delta risk reversal = IV at the strike nearest +0.25 call_delta minus IV
    at the strike nearest -0.25 put_delta. None when the chain has no strike within
    `tol` of ±0.25 on either side (too sparse / too short-dated). Sign by
    construction: >0 call-skew (bullish lean), <0 put-skew (defensive)."""
    best_c = best_p = None   # (delta_distance, iv)
    for r in (greeks_rows or []):
        cd, cv = _f(r.get("call_delta")), _f(r.get("call_volatility"))
        pd, pv = _f(r.get("put_delta")), _f(r.get("put_volatility"))
        if cd is not None and cv is not None:
            d = abs(cd - 0.25)
            if d <= tol and (best_c is None or d < best_c[0]):
                best_c = (d, cv)
        if pd is not None and pv is not None:
            d = abs(pd - (-0.25))
            if d <= tol and (best_p is None or d < best_p[0]):
                best_p = (d, pv)
    if best_c is None or best_p is None:
        return None
    return best_c[1] - best_p[1]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_verdict.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/verdict.py tests/test_verdict.py
git commit -m "feat(verdict): derive_rr25 — 25-delta risk reversal from greeks (pure)"
```

---

### Task 2: `skew_state` (pure)

**Files:**
- Modify: `server/verdict.py`
- Test: `tests/test_verdict.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_verdict.py  (append)
def test_skew_state_calls():
    assert verdict.skew_state(0.05, "calls") == "agree"     # RR>thr, calls → agree
    assert verdict.skew_state(-0.05, "calls") == "oppose"   # put-skew vs long calls → oppose
    assert verdict.skew_state(0.001, "calls") == "neutral"  # inside threshold
    assert verdict.skew_state(None, "calls") == "unavailable"


def test_skew_state_puts_mirrored():
    assert verdict.skew_state(-0.05, "puts") == "agree"     # put-skew supports puts
    assert verdict.skew_state(0.05, "puts") == "oppose"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_verdict.py -k skew_state -q`
Expected: FAIL — `skew_state` not defined.

- [ ] **Step 3: Implement** (append to `server/verdict.py`)

```python
def skew_state(rr25: float | None, direction: str, *, thr: float = _SKEW_THR) -> str:
    """agree | oppose | neutral | unavailable. Asymmetric oppose-veto: agreement
    is mild corroboration (handled by the caller as subordinate), opposition is the
    load-bearing caution. calls want RR>0 (call-skew); puts want RR<0 (put-skew)."""
    if rr25 is None:
        return "unavailable"
    if abs(rr25) < thr:
        return "neutral"
    bullish = rr25 > 0
    if direction == "calls":
        return "agree" if bullish else "oppose"
    return "oppose" if bullish else "agree"   # puts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_verdict.py -k skew_state -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/verdict.py tests/test_verdict.py
git commit -m "feat(verdict): skew_state — asymmetric agree/oppose/neutral/unavailable"
```

---

### Task 3: `positioning_leg` (pure)

**Files:**
- Modify: `server/verdict.py`
- Test: `tests/test_verdict.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_verdict.py  (append)
def test_positioning_green_only_on_opening_flow_even_without_archive():
    # decoupling guard: opening flow + green flow gate → green even when OI unconfirmed
    assert verdict.positioning_leg("opening_flow", "green", "unconfirmed") == "green"


def test_positioning_total_flow_caps_at_yellow():
    assert verdict.positioning_leg("total_flow", "green", "building") == "yellow"


def test_positioning_unwinding_caps_green_to_yellow():
    assert verdict.positioning_leg("opening_flow", "green", "unwinding") == "yellow"


def test_positioning_gamma_fallback_is_red():
    assert verdict.positioning_leg("gamma_fallback", "green", "building") == "red"


def test_positioning_weak_flow_yellow():
    assert verdict.positioning_leg("opening_flow", "yellow", "building") == "yellow"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_verdict.py -k positioning -q`
Expected: FAIL — `positioning_leg` not defined.

- [ ] **Step 3: Implement** (append to `server/verdict.py`)

```python
def positioning_leg(direction_basis: str, flow_gate: str, oi_confirmation: str) -> str:
    """green / yellow / red. Collapses Flow+OI into the predictive core. Green
    requires the OPENING basis (not the weaker total_flow fallback) so a Favorable
    verdict can't rest on it; archive-decoupled (green even when OI 'unconfirmed');
    'unwinding' caps green→yellow."""
    if direction_basis == "gamma_fallback" or flow_gate == "red":
        return "red"
    if direction_basis == "total_flow":
        return "yellow"                       # weaker basis — caps below Favorable
    # opening_flow:
    if flow_gate == "green" and oi_confirmation != "unwinding":
        return "green"
    return "yellow"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_verdict.py -k positioning -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/verdict.py tests/test_verdict.py
git commit -m "feat(verdict): positioning_leg — opening-flow green, total_flow caps yellow"
```

---

### Task 4: `compute_verdict` + action (pure)

**Files:**
- Modify: `server/verdict.py`
- Test: `tests/test_verdict.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_verdict.py  (append)
def _v(**kw):
    base = dict(direction="calls", direction_basis="opening_flow", flow_gate="green",
                structural_gate="green", oi_confirmation="building", rr25=0.05, cost_gate="green")
    base.update(kw)
    return verdict.compute_verdict(**base)


def test_compute_verdict_favorable_happy_path():
    v = _v()
    assert v["positioning"] == "green" and v["overall"] == "Favorable"
    assert v["action"].startswith("Worth acting on")
    assert v["signal_conflict"] is False


def test_compute_verdict_skew_oppose_conflicts_and_caps():
    v = _v(rr25=-0.05)                       # put-skew vs long calls → oppose
    assert v["skew"] == "oppose"
    assert v["signal_conflict"] is True and "skew" in v["conflict_legs"]
    assert v["overall"] == "Mixed" and v["action"] == "Skip — signals disagree"


def test_compute_verdict_skew_agree_never_favorable_alone():
    # total_flow positioning (yellow) + skew agree must NOT become Favorable
    v = _v(direction_basis="total_flow", rr25=0.05)
    assert v["skew"] == "agree" and v["overall"] != "Favorable"


def test_compute_verdict_cost_block_stand_down():
    v = _v(cost_gate="red")
    assert v["cost_guard"] == "block" and v["overall"] == "Stand down"
    assert v["action"] == "Stand down"


def test_compute_verdict_structural_red_conflict():
    v = _v(structural_gate="red")
    assert v["signal_conflict"] is True and "structural" in v["conflict_legs"]
    assert v["overall"] == "Mixed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_verdict.py -k compute_verdict -q`
Expected: FAIL — `compute_verdict` not defined.

- [ ] **Step 3: Implement** (append to `server/verdict.py`)

```python
def compute_verdict(*, direction: str, direction_basis: str, flow_gate: str,
                    structural_gate: str, oi_confirmation: str,
                    rr25: float | None, cost_gate: str) -> dict:
    positioning = positioning_leg(direction_basis, flow_gate, oi_confirmation)
    skew = skew_state(rr25, direction)
    cost_guard = {"green": "ok", "yellow": "caution", "red": "block"}.get(cost_gate, "caution")

    conflict_legs = []
    if positioning in ("green", "yellow"):           # only conflicts when we have a side
        if structural_gate == "red":
            conflict_legs.append("structural")
        if skew == "oppose":
            conflict_legs.append("skew")
    signal_conflict = bool(conflict_legs)

    if positioning == "red" or cost_guard == "block":
        overall = "Stand down"
    elif positioning == "green" and not signal_conflict and cost_guard == "ok" and skew != "oppose":
        overall = "Favorable"
    else:
        overall = "Mixed"

    if overall == "Favorable":
        action = "Worth acting on — the rare one"
    elif overall == "Stand down":
        action = "Stand down"
    elif signal_conflict:
        action = "Skip — signals disagree"
    else:
        action = "Wait — not compelling"

    return {
        "positioning": positioning, "structural": structural_gate, "skew": skew,
        "cost_guard": cost_guard, "signal_conflict": signal_conflict,
        "conflict_legs": conflict_legs, "overall": overall, "action": action,
        "rr25": rr25,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_verdict.py -q`
Expected: PASS (all verdict tests).

- [ ] **Step 5: Commit**

```bash
git add server/verdict.py tests/test_verdict.py
git commit -m "feat(verdict): compute_verdict — legs + signal_conflict + overall + action"
```

---

### Task 5: `Verdict` schema + `Row.verdict`

**Files:**
- Modify: `server/schema.py`
- Test: `tests/test_schema.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema.py  (append)
def test_verdict_model_and_row_field_default_none():
    from server.schema import Verdict, Row, Gates, GateMethod
    v = Verdict(positioning="green", structural="green", skew="agree",
                cost_guard="ok", overall="Favorable", action="Worth acting on")
    assert v.signal_conflict is False and v.conflict_legs == [] and v.rr25 is None
    r = Row(ticker="AAPL", spot=1.0, direction="calls",
            gates=Gates(flow="green", oi="green", structural="green", cost="green"),
            gate_method=GateMethod(flow="absolute", oi="absolute", structural="absolute", cost="absolute"))
    assert r.verdict is None     # not computed on light/plain rows
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_schema.py::test_verdict_model_and_row_field_default_none -q`
Expected: FAIL — `Verdict` not defined.

- [ ] **Step 3: Implement** — add to `server/schema.py` (after the `Gates`/`GateMethod` block, before `Row`):

```python
class Verdict(BaseModel):
    positioning: Literal["green", "yellow", "red"]
    structural: Literal["green", "yellow", "red"]
    skew: Literal["agree", "oppose", "neutral", "unavailable"]
    cost_guard: Literal["ok", "caution", "block"]
    signal_conflict: bool = False
    conflict_legs: list[str] = Field(default_factory=list)
    overall: Literal["Favorable", "Mixed", "Stand down"]
    action: str
    rr25: float | None = None
```

Add to the `Row` model (next to `is_light`):

```python
    verdict: Verdict | None = None   # deep-dive 3-leg verdict; None on light/grid rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_schema.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/schema.py tests/test_schema.py
git commit -m "feat(schema): Verdict model + Row.verdict (deep-dive only)"
```

---

### Task 6: Wire `build_single_row` to compute `row.verdict`

**Files:**
- Modify: `server/snapshot.py` — add `_skew_expiry`; set `row.verdict` before `return row` (~line 356)
- Test: `tests/test_snapshot.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_snapshot.py  (append)
async def test_build_single_row_sets_verdict(stub_uw, fresh_storage_state, tmp_data_dir):
    from server import snapshot as snap
    row = await snap.build_single_row("SPY")
    assert row is not None
    assert row.verdict is not None
    assert row.verdict.overall in ("Favorable", "Mixed", "Stand down")
    assert row.verdict.positioning in ("green", "yellow", "red")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_snapshot.py::test_build_single_row_sets_verdict -q`
Expected: FAIL — `row.verdict` is None (not yet computed).

- [ ] **Step 3: Implement** — in `server/snapshot.py`:

Add the import near the top (with the other `from server import …`):

```python
from server import verdict as verdict_mod
```

Add a skew-expiry helper (near `_is_opex_week`):

```python
def _skew_expiry(d) -> str:
    """3rd-Friday monthly expiry >= ~25 DTE out — the ~30d skew horizon (less
    noisy than the weekly wings). Monthlies exist for any liquid optionable name;
    a name without it → greeks 404 → skew 'unavailable' (graceful)."""
    import calendar
    from datetime import date, timedelta
    def third_friday(y, m):
        first = date(y, m, 1)
        offset = (calendar.FRIDAY - first.weekday()) % 7
        return first + timedelta(days=offset + 14)
    cand = third_friday(d.year, d.month)
    if (cand - d).days < 25:
        ny, nm = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        cand = third_friday(ny, nm)
    return cand.isoformat()
```

In `build_single_row`, immediately before `row.insights = ...` / `return row` (~line 356), add:

```python
    # Deep-dive 3-leg verdict (Plan 3). Skew is derived from ~30d greeks (its own
    # expiry, ~1 extra call; cheap, deep-dive only). Sparse/missing greeks → skew
    # 'unavailable', verdict still computes from positioning + structural + cost.
    try:
        gx = await _in_ctx(loop, partial(storage.fetch_greeks, ticker,
                                         _skew_expiry(datetime.now(tz=timezone.utc).date())))
        gx_rows = gx.get("data") if isinstance(gx, dict) else None
        rr25 = verdict_mod.derive_rr25(gx_rows or [])
    except Exception as e:
        log.warning("verdict skew fetch failed for %s: %s", ticker, e)
        rr25 = None
    row.verdict = Verdict(**verdict_mod.compute_verdict(
        direction=row.direction, direction_basis=row.direction_basis,
        flow_gate=row.gates["flow"], structural_gate=row.gates["structural"],
        oi_confirmation=row.tile2.confirmation, rr25=rr25, cost_gate=row.gates["cost"]))
```

Add `Verdict` to the schema import at the top of `snapshot.py` (the `from server.schema import …` line).

Notes for the implementer:
- `row.gates` is a plain dict (`{"flow":..., "structural":..., "cost":...}`) as built earlier in this function — index with `["flow"]` etc.
- `storage.fetch_greeks(ticker, expiry)` already exists; in REPLAY/cached_only it reads the archive (graceful → likely None → skew unavailable).
- Do NOT set verdict in `build_light_snapshot` / `_light_row` (stays None).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_snapshot.py::test_build_single_row_sets_verdict -q`
Expected: PASS. Then full file: `.venv/Scripts/python -m pytest tests/test_snapshot.py -q` (fix any stub_uw greeks-fetch gaps — the stub may need a greeks response; if it lacks one, `rr25` is None and the verdict still computes, which is fine).

- [ ] **Step 5: Commit**

```bash
git add server/snapshot.py tests/test_snapshot.py
git commit -m "feat(snapshot): build_single_row computes row.verdict (skew from ~30d greeks)"
```

---

### Task 7: Deep-dive 3-leg verdict panel (frontend)

**Files:**
- Modify: `static/index.html` — `renderDeepDive` / a new `renderVerdictPanel(row)` rendered inside the deep-dive card
- Test: `tests/test_html_preservation.py` (keep green)

- [ ] **Step 1: Add the verdict panel renderer**

In `static/index.html`, add a renderer and call it at the top of the deep-dive card body (inside `renderAllTiles(row)` or `renderDeepDive`, above the tiles):

```javascript
function renderVerdictPanel(row) {
  const v = row.verdict;
  if (!v) return "";                       // light/plain rows have no verdict
  const dot = c => `<span class="dot ${c}"></span>`;
  const skewCls = v.skew === "oppose" ? "neg" : v.skew === "agree" ? "ok" : "axis";
  const conflict = v.signal_conflict
    ? `<div class="vd-conflict">⚠ Signals disagree (${v.conflict_legs.join(", ")}) — positioning says ${row.direction}</div>`
    : "";
  const actCls = v.overall === "Favorable" ? "ok" : v.overall === "Stand down" ? "neg" : "warn";
  return `
    <div class="verdict-panel">
      <div class="vd-action ${actCls}">${v.action}</div>
      <div class="vd-legs">
        <span class="vd-leg">${dot(v.positioning)} Positioning</span>
        <span class="vd-leg">${dot(v.structural)} Structural</span>
        <span class="vd-leg vd-skew" title="25Δ risk-reversal (derived, ~30d) — corroboration only">
          <em>${v.skew}</em> skew</span>
        <span class="vd-leg vd-guard">cost: ${v.cost_guard}</span>
      </div>
      ${conflict}
    </div>`;
}
```

Add CSS (near the other deep-dive styles):

```css
  .verdict-panel { border: 1px solid var(--axis); border-radius: 8px; padding: 10px 12px; margin-bottom: 10px; }
  .vd-action { font-family: var(--ff-display); font-weight: 700; font-size: 15px; margin-bottom: 6px; }
  .vd-action.ok { color: var(--ok); } .vd-action.warn { color: var(--warn); } .vd-action.neg { color: var(--neg); }
  .vd-legs { display: flex; gap: 14px; flex-wrap: wrap; align-items: center; font-size: 12px; color: var(--text-dim); }
  .vd-skew { opacity: 0.7; font-size: 11px; }      /* subordinate — never a peer green */
  .vd-guard { opacity: 0.8; }
  .vd-conflict { color: var(--warn); font-weight: 700; margin-top: 6px; font-size: 12px; }
```

Render it: in `renderAllTiles(row)`, prefix the returned markup with `renderVerdictPanel(row)` so the panel sits atop the tiles.

- [ ] **Step 2: Verify offline (replay + browser)**

Run a replay server; click a ticker; confirm the verdict panel shows the action headline, three legs with **skew visually subordinate** (smaller/muted, not a peer dot), the cost guard, and a conflict banner when legs disagree. Inject a verdict via `browser_evaluate` if the replay archive can't build one (same technique used for prior frontend verification).

```bash
DATA_DIR=./data REPLAY=1 .venv/Scripts/python -m uvicorn server.main:app --port 8015
```

- [ ] **Step 3: Keep preservation test green**

Run: `.venv/Scripts/python -m pytest tests/test_html_preservation.py -q`
Expected: PASS (6 zones intact, render functions present). If a guarded anchor moved, update the test to match intent.

- [ ] **Step 4: Commit**

```bash
git add static/index.html tests/test_html_preservation.py
git commit -m "feat(ui): deep-dive verdict panel — action headline + 3 legs (skew subordinate) + conflict banner"
```

---

### Task 8: Verify + finish

- [ ] **Step 1: Full suite** — `.venv/Scripts/python -m pytest -q` (expect all pass, live skipped).
- [ ] **Step 2: Call-count sanity** — confirm `build_single_row` adds exactly one greeks fetch for the skew leg (the ~30d expiry); light rows still compute no verdict.
- [ ] **Step 3: REPLAY smoke + screenshot** of the verdict panel (action + 3 legs + conflict).
- [ ] **Step 4: Update docs** — note `row.verdict` + the deep-dive verdict panel in README/CLAUDE.md architecture (one line each).
- [ ] **Step 5: Finish** — use **superpowers:finishing-a-development-branch** (verify tests → present 4 options → execute). Expected: merge to main + push.

---

## Deferred / out of scope
- The `realized_vol` regime fix (separate commit: keys `implied_volatility`/`realized_volatility`, read latest settled row).
- `_SKEW_THR` (0.02) calibration once live skew distributions are observed.
- Promoting the verdict to the light grid (deliberately deep-dive only).

## Self-Review
**Spec coverage:** derive_rr25 (T1) · skew_state asymmetric (T2) · positioning collapse w/ opening_flow-green + total_flow-yellow + unwinding-cap + decoupling (T3) · compute_verdict legs/conflict/overall/action, skew-agree-never-Favorable, cost-block→Stand-down (T4) · Verdict schema + Row.verdict None default (T5) · build_single_row wiring + ~30d skew expiry + graceful skew-unavailable + light rows None (T6) · deep-dive panel, skew subordinate, conflict banner, action headline, grid untouched (T7) · verify/docs/finish (T8). All spec sections map to a task.
**Placeholder scan:** every code step has complete code; frontend verified via replay+browser (the repo's actual frontend method), stated honestly.
**Type consistency:** `compute_verdict` returns the dict whose keys exactly match `Verdict(**…)` fields (positioning/structural/skew/cost_guard/signal_conflict/conflict_legs/overall/action/rr25); `derive_rr25`/`skew_state`/`positioning_leg` signatures match their call sites in T4/T6; `row.gates` indexed as a dict; `Tile2.confirmation` values match `positioning_leg`'s `unwinding` check.
