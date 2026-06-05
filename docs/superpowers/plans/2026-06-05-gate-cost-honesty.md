# Gate & Cost Honesty (#4 + Cost + #6) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the structural gate regime-aware (positive gamma can't flash green), add an explicit expected-move-vs-round-trip-cost check to Tile 4, and delete the dead client-side gate recomputation so the server is the unambiguous single source of truth.

**Architecture:** Three independent slices. #4 caps `gates._structural_gate` at yellow when `gex_sign=="POS"` (server-only; the client already renders `row.gates`). Cost adds a `cost_check` block to `tile4.build_tile4` from data it already computes (`expected_move_pct`, top-contract `spread_pct`/`theta`/premium) and renders it in the Tile 4 view. #6 removes the never-called `gateStatesLive`/`gateStatesSynth` (+ `enrichLive`/`normalizeSynth` if confirmed dead) and the misleading `gates.oi="yellow"` hardcode, leaving `CALIBRATION` (still-used display keys) intact.

**Tech Stack:** Python 3.11, pydantic v2, pytest; vanilla-JS frontend (static/index.html).

**Spec:** `docs/superpowers/specs/2026-06-04-signal-honesty-design.md`. This plan = #4 + Cost + #6. The skew leg + Positioning collapse (#5) is a separate Plan 3 (it needs the live risk-reversal sign probe).

---

## File Structure

- **Modify** `server/gates.py` — `_structural_gate` reads `gex_sign`, caps green→yellow on positive gamma.
- **Modify** `server/tile4.py` — `build_tile4` computes a `cost_check` dict (expected move vs round-trip cost) for the top contract; returned in the payload.
- **Modify** `static/index.html` — render the cost_check line in the Tile 4 view; delete the dead gate-recompute functions.
- **Tests:** `tests/test_gates.py`, `tests/test_tile4.py`, `tests/test_html_preservation.py`.

---

## Task 1: #4 — gamma sign caps the structural gate

**Files:**
- Modify: `server/gates.py` (`_structural_gate`, ~lines 113-124)
- Test: `tests/test_gates.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_gates.py`:

```python
def _struct_row(**kw):
    base = {"flip_dist_pct": 1.0, "direction": "calls",
            "wall_up_dist_pct": 5.0, "wall_dn_dist_pct": 5.0, "gex_sign": "NEG"}
    base.update(kw)
    return base


def test_structural_green_when_negative_gamma_and_flip_near():
    # NEG gamma + flip within 1.5% on a clear side -> green allowed
    assert gates._structural_gate(_struct_row(gex_sign="NEG", flip_dist_pct=1.0)) == "green"


def test_structural_positive_gamma_caps_green_to_yellow():
    # Same near-flip setup, but POS gamma (chop/pin) -> capped at yellow
    assert gates._structural_gate(_struct_row(gex_sign="POS", flip_dist_pct=1.0)) == "yellow"


def test_structural_positive_gamma_leaves_red_as_red():
    # Wall too close -> red; POS cap is a ceiling (yellow), red stays red
    row = _struct_row(gex_sign="POS", direction="calls", wall_up_dist_pct=0.5, flip_dist_pct=1.0)
    assert gates._structural_gate(row) == "red"


def test_structural_missing_gex_sign_behaves_as_before():
    # No gex_sign key -> not POS -> no cap (back-compat)
    row = _struct_row(flip_dist_pct=1.0)
    del row["gex_sign"]
    assert gates._structural_gate(row) == "green"
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/test_gates.py -k structural -q`
Expected: FAIL on `test_structural_positive_gamma_caps_green_to_yellow` (currently returns "green" — `gex_sign` isn't read).

- [ ] **Step 3: Implement — update `_structural_gate` in `server/gates.py`**

Replace the existing function body's final logic. The current function is:
```python
def _structural_gate(row: dict) -> Color:
    flip = abs(row.get("flip_dist_pct", 99))
    direction = row.get("direction", "calls")
    dir_wall_pct = (row.get("wall_up_dist_pct", 99) if direction == "calls"
                    else row.get("wall_dn_dist_pct", 99))
    if dir_wall_pct <= GATE_THRESHOLDS["gate3_wall_dist_pct"]:
        return "red"
    if flip <= GATE_THRESHOLDS["gate3_flip_dist_pct"]:
        return "green"
    if flip <= GATE_THRESHOLDS["gate3_flip_dist_pct"] * 2:
        return "yellow"
    return "red"
```
Replace it with:
```python
def _structural_gate(row: dict) -> Color:
    flip = abs(row.get("flip_dist_pct", 99))
    direction = row.get("direction", "calls")
    dir_wall_pct = (row.get("wall_up_dist_pct", 99) if direction == "calls"
                    else row.get("wall_dn_dist_pct", 99))
    if dir_wall_pct <= GATE_THRESHOLDS["gate3_wall_dist_pct"]:
        return "red"
    if flip <= GATE_THRESHOLDS["gate3_flip_dist_pct"]:
        color: Color = "green"
    elif flip <= GATE_THRESHOLDS["gate3_flip_dist_pct"] * 2:
        color = "yellow"
    else:
        return "red"
    # Regime cap: positive dealer gamma (POS = long γ → chop/pin, premium-killing)
    # can't justify a GREEN directional-weekly structural read. Cap at yellow.
    if color == "green" and row.get("gex_sign") == "POS":
        return "yellow"
    return color
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/test_gates.py -k structural -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the full gates file + snapshot (structural feeds the row build)**

Run: `python -m pytest tests/test_gates.py tests/test_snapshot.py -q`
Expected: all pass. (Note: `snapshot.py` line ~248 already forces structural=yellow when `gex_status != "ok"`; this cap is additive and independent.)

- [ ] **Step 6: Commit**

```bash
git add server/gates.py tests/test_gates.py
git commit -m "feat(gates): positive gamma caps the structural gate at yellow (#4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Cost — expected move vs round-trip cost (Tile 4)

**Files:**
- Modify: `server/tile4.py` (`build_tile4`, the `ok` return ~line 369-373)
- Test: `tests/test_tile4.py`

Round-trip cost for the top contract = its bid/ask spread (a full spread ≈ enter+exit) + theta paid over a short hold. Compare to the at-the-money expected move. The top ranked contract already carries `ask`, `spread_pct`, and `theta`; the view carries `expected_move_pct`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_tile4.py`:

```python
def test_build_tile4_cost_check_present_and_flags_uncovered(stub4, monkeypatch):
    """Tile 4 surfaces whether the expected move can clear the round-trip cost
    (spread + ~3d theta) of the top contract."""
    out = tile4.build_tile4("SPY", _ctx4())
    assert out["status"] == "ok"
    cc = out.get("cost_check")
    assert cc is not None
    assert "expected_move_pct" in cc and "round_trip_cost_pct" in cc and "covers" in cc
    assert isinstance(cc["covers"], bool)
    # The stub's expected move (6%) comfortably clears a tight-spread contract
    assert cc["covers"] is True


def test_build_tile4_cost_check_none_when_no_expected_move(stub4, monkeypatch):
    """If expected move can't be computed, cost_check is reported as unknown,
    not a false 'covers'."""
    monkeypatch.setattr(tile4, "_expected_move_pct", lambda atm, spot: None)
    out = tile4.build_tile4("SPY", _ctx4())
    cc = out.get("cost_check")
    assert cc is not None and cc["covers"] is None and cc["expected_move_pct"] is None
```

- [ ] **Step 2: Run to verify FAIL**

Run: `python -m pytest tests/test_tile4.py -k cost_check -q`
Expected: FAIL — `out["cost_check"]` is None / missing.

- [ ] **Step 3: Implement — add `_cost_check` helper + wire into `build_tile4`**

In `server/tile4.py`, add this helper (near `_expected_move_pct`):
```python
_HOLD_DAYS = 3  # rough weekly-hold horizon for the theta-drag estimate

def _cost_check(top: dict | None, expected_move_pct: float | None) -> dict:
    """Can the expected move clear the top contract's round-trip cost?
    cost ≈ full bid/ask spread (% of mid) + theta paid over ~_HOLD_DAYS, as a %
    of premium. covers=None when we can't compute the move (honest unknown)."""
    spread = (top or {}).get("spread_pct")
    theta = (top or {}).get("theta")
    prem = (top or {}).get("ask") or (top or {}).get("mid")
    theta_drag = None
    if theta is not None and prem:
        theta_drag = abs(theta) * _HOLD_DAYS / prem * 100.0
    rt = round((spread or 0.0) + (theta_drag or 0.0), 1) if (spread is not None or theta_drag is not None) else None
    covers = None
    if expected_move_pct is not None and rt is not None:
        covers = expected_move_pct >= rt
    return {
        "expected_move_pct": expected_move_pct,
        "round_trip_cost_pct": rt,
        "spread_pct": spread,
        "theta_drag_pct": round(theta_drag, 1) if theta_drag is not None else None,
        "hold_days": _HOLD_DAYS,
        "covers": covers,
    }
```
Then in `build_tile4`, change the final `ok` return (currently):
```python
    ranked = rank_contracts(scored)
    out = {
        "status": "stand_down" if gates["stand_down"] else "ok",
        "ticker": ticker, "direction": direction, "expiry": expiry,
        "gates": gates, "term_curve": term_curve, "expected_move_pct": expected_move_pct,
        "ranked": ranked, "top": ranked[0] if ranked else None,
    }
```
to add the cost_check (insert the `cost_check` line):
```python
    ranked = rank_contracts(scored)
    out = {
        "status": "stand_down" if gates["stand_down"] else "ok",
        "ticker": ticker, "direction": direction, "expiry": expiry,
        "gates": gates, "term_curve": term_curve, "expected_move_pct": expected_move_pct,
        "ranked": ranked, "top": ranked[0] if ranked else None,
        "cost_check": _cost_check(ranked[0] if ranked else None, expected_move_pct),
    }
```

- [ ] **Step 4: Run to verify PASS**

Run: `python -m pytest tests/test_tile4.py -k cost_check -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full tile4 file**

Run: `python -m pytest tests/test_tile4.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add server/tile4.py tests/test_tile4.py
git commit -m "feat(tile4): expected-move-vs-round-trip-cost check (Cost guard)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Render the cost_check line in Tile 4

**Files:**
- Modify: `static/index.html` (`renderTile4Picker`)
- Test: `tests/test_html_preservation.py`

- [ ] **Step 1: Add the cost line to the Tile 4 legend/footer area**

In `static/index.html`, in `renderTile4Picker`, find the `t4-legend` block (the "Ranked best→worst…" div). Immediately BEFORE that `<div class="t4-legend">` line, insert a cost-check line built from `t4.cost_check`:

```javascript
  const cc = t4.cost_check || {};
  const ccLine = (cc.covers === null || cc.covers === undefined)
    ? `<div class="t4-cost unknown">Move-vs-cost: expected move unavailable — can't judge if it pays.</div>`
    : `<div class="t4-cost ${cc.covers ? "ok" : "bad"}">Expected move ${cc.expected_move_pct != null ? cc.expected_move_pct.toFixed(1) : "—"}% vs round-trip cost ~${cc.round_trip_cost_pct != null ? cc.round_trip_cost_pct.toFixed(1) : "—"}% (spread ${cc.spread_pct != null ? cc.spread_pct.toFixed(1) : "—"}% + ~${cc.theta_drag_pct != null ? cc.theta_drag_pct.toFixed(1) : "—"}% θ/${cc.hold_days}d) — ${cc.covers ? "the move can pay for the trade." : "the move likely can't pay the cost — caution."}</div>`;
```
Then insert `${ccLine}` into the returned template, immediately before `${badges}` or right after the `</h3>` (pick the spot right after the closing `</h3>` of the Tile 4 header so it reads near the top). Concretely, in the big return template change:
```javascript
      <button class="help-icon" data-help="tile4">?</button>${staleTag}${asOfTag}
    </h3>
    ${badges}
```
to:
```javascript
      <button class="help-icon" data-help="tile4">?</button>${staleTag}${asOfTag}
    </h3>
    ${ccLine}
    ${badges}
```

- [ ] **Step 2: Add CSS for `.t4-cost`**

Near the other `.t4-*` styles, add:
```css
  .t4-cost { font-size: 10px; font-family: var(--ff-mono); margin: 6px 0; padding: 4px 8px; border-radius: 4px; }
  .t4-cost.ok { color: var(--ok); background: rgba(63,185,80,0.08); }
  .t4-cost.bad { color: var(--warn); background: rgba(210,153,34,0.10); }
  .t4-cost.unknown { color: var(--axis); }
```

- [ ] **Step 3: Run preservation test**

Run: `python -m pytest tests/test_html_preservation.py -q`
Expected: PASS (additions only).

- [ ] **Step 4: Commit**

```bash
git add static/index.html
git commit -m "feat(tile4): render expected-move-vs-cost line in the picker

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: #6 — delete the dead client-side gate recomputation

**Files:**
- Modify: `static/index.html` (remove `gateStatesLive`, `gateStatesSynth`, and `enrichLive`/`normalizeSynth` IF confirmed uncalled)
- Test: `tests/test_html_preservation.py`

- [ ] **Step 1: Confirm the functions are dead (no callers)**

Run:
```bash
grep -n "gateStatesLive\|gateStatesSynth\|enrichLive(\|normalizeSynth(" static/index.html
```
Expected: each name appears ONLY on its `function <name>(` definition line (and possibly inside a comment). If any name has a real CALL site (e.g., `ROWS.map(enrichLive)`, `= gateStatesLive(`), DO NOT delete that function — note it and leave it. Proceed to delete only the confirmed-dead ones. (Per investigation, all four are uncalled — every render path reads server `row.gates` directly — but verify before cutting.)

- [ ] **Step 2: Delete the confirmed-dead functions**

For each confirmed-dead function (`gateStatesLive`, `gateStatesSynth`, and `enrichLive`/`normalizeSynth` if uncalled), delete its entire `function …(row) { … }` body from `static/index.html`. This removes the misleading `gates.oi = "yellow"  // honestly: we don't have OI-change for live rows` hardcode (it lives inside `gateStatesLive`). Leave the `CALIBRATION` object intact — its remaining keys (e.g., `gate3_flip_dist_pct` used live at the Tile 3 render, plus display keys) are still referenced. If a comment elsewhere references the deleted functions (e.g., "set by enrichLive / normalizeSynth"), reword it to "set by the server row shape".

- [ ] **Step 3: Verify nothing references the removed names**

Run:
```bash
grep -n "gateStatesLive\|gateStatesSynth\|enrichLive\|normalizeSynth" static/index.html
```
Expected: ZERO matches (or only an unrelated reworded comment with none of the function names). If a name still appears as a call, you deleted a live function — restore it and reassess.

- [ ] **Step 4: Run preservation + full suite**

Run: `python -m pytest tests/test_html_preservation.py -q`
Expected: PASS. The required-function list (`renderTile1Flow`, `renderTile2OI`, `renderTile3Structural`, `renderTile6Picker`, `ladderSvg`, `renderFlowChart`) does NOT include the deleted ones, so it stays green. The v1-floor test is skipped when the v1 prototype isn't adjacent; if it runs and trips on the lowered function count, that's sanctioned dead-code removal — retune that specific floor assertion to the new count with a one-line note (per CLAUDE.md).

- [ ] **Step 5: Commit**

```bash
git add static/index.html tests/test_html_preservation.py
git commit -m "refactor(#6): delete dead client gate recompute — server is the single source

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Verification (offline + browser)

**Files:** none.

- [ ] **Step 1: Full suite**

Run: `python -m pytest -q`
Expected: all pass (live skipped, 1 xfail), count ≥ prior baseline + new tests.

- [ ] **Step 2: Replay smoke + browser**

Run (background, PowerShell): `$env:DATA_DIR="./data"; $env:REPLAY="1"; .venv/Scripts/python -m uvicorn server.main:app --port 8000`
- `Invoke-RestMethod http://localhost:8000/api/tile4/SPY | Select-Object status,cost_check` → cost_check populated (or `covers:null` honestly if no expected move in replay).
- Browser (Playwright): open the dashboard, click a ticker, confirm the Tile 4 cost line renders and the structural gate dot reflects the server value (no client recompute). Screenshot.

- [ ] **Step 3: Final commit if fixups**

```bash
git add -A && git commit -m "test: offline verification of gate/cost honesty"
```

---

## Self-Review

**Spec coverage:**
- #4 gex_sign caps structural at yellow → Task 1 (green→yellow on POS; red stays red; missing-sign back-compat). ✓
- Cost expected-move-vs-round-trip-cost owned by the Cost guard → Task 2 (`_cost_check` in tile4) + Task 3 (render). `covers=None` honest-unknown when no expected move. ✓
- #6 single source of truth → Task 4 (delete dead recompute + the `gates.oi="yellow"` hardcode; server is the only source). ✓
- Deferred to Plan 3 (correctly NOT here): #5 skew leg + Positioning collapse + signal_conflict (needs the live RR-sign probe).

**Placeholder scan:** none — full code in every step. Task 4 is guarded (verify-dead-before-delete) rather than a blind change.

**Type consistency:** `_structural_gate(row)->Color` unchanged signature (Task 1). `_cost_check(top, expected_move_pct)->dict` defined and called identically (Task 2); the `cost_check` dict keys (`expected_move_pct`, `round_trip_cost_pct`, `spread_pct`, `theta_drag_pct`, `hold_days`, `covers`) match between Task 2 (producer) and Task 3 (renderer). `covers` is `bool|None` consistently.
