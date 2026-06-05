# Market Regime Header — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the static hand-typed regime banner with a live, computed market-regime read — SPY gamma-regime headline, economic-calendar event veto (hold-window aware), buyer-framed vol, tide/OPEX badges — synthesized into a Favorable/Mixed/Stand-down posture that's a *regime read, never a direction call*, and that also feeds the per-ticker cost gate.

**Architecture:** A pure `server/market_regime.py` (`compute_market_regime`) holds all synthesis + framing logic (heavily unit-tested, incl. the no-direction guard). `snapshot.py` fetches the market inputs once per cycle (SPY gex, SPY vol, market_tide, economic_calendar), calls it, attaches the structured result to `Snapshot.regime`, and threads its `event_within_hold` flag into each row's cost gate. The frontend renders the structured regime.

**Tech Stack:** Python 3.11, pydantic v2, pytest; vanilla-JS frontend (static/index.html).

**Spec:** `docs/superpowers/specs/2026-06-05-market-regime-header-design.md`.

---

## File Structure
- **Create** `server/market_regime.py` — pure `compute_market_regime(...)` → structured dict. No I/O.
- **Modify** `server/uw.py` — `fetch_economic_calendar()`.
- **Modify** `server/storage.py` — `fetch_economic_calendar()` wrapper (1h TTL).
- **Modify** `server/schema.py` — extend `Regime` with structured fields.
- **Modify** `server/gates.py` — `_cost_gate(row, event_within_hold=False)` macro-event input.
- **Modify** `server/snapshot.py` — build market inputs, compute regime, attach, thread event flag into row gates.
- **Modify** `static/index.html` — `renderRegimeBanner` renders the structured regime.
- **Modify** `tests/contracts.py` + `tests/test_uw_contracts.py` — economic_calendar contract + live probe + SPY-GEX population probe.
- **Tests:** `tests/test_market_regime.py` (new), `tests/test_gates.py`, `tests/test_schema.py`, `tests/test_snapshot.py`, `tests/test_html_preservation.py`.

---

## Task 1: `compute_market_regime` pure module

**Files:** Create `server/market_regime.py`; Test `tests/test_market_regime.py`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_market_regime.py`:

```python
from datetime import datetime, timezone, timedelta
from server import market_regime as mr


def _now():
    return datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc)


def _ev(name, days, type_="report", hour=13):
    t = (_now() + timedelta(days=days)).replace(hour=hour, minute=30)
    return {"event": name, "time": t.isoformat(), "type": type_}


def _call(**kw):
    base = dict(gamma={"sign": "NEG", "flip_pct": 1.0, "status": "ok"},
                vol={"iv": 0.18, "rv": 0.20, "trend": None},
                events=[], tide={"lean": "neutral"}, opex=False, now=_now())
    base.update(kw)
    return mr.compute_market_regime(**base)


def test_trend_regime_headline_and_favorable():
    out = _call(gamma={"sign": "NEG", "flip_pct": 1.0, "status": "ok"})
    assert "trend" in out["headline"].lower()
    assert out["posture"] == "Favorable"


def test_chop_regime_stands_down_on_its_own():
    # POS gamma, no event, neutral tide, vol not a clear lift -> Stand down (key calibration)
    out = _call(gamma={"sign": "POS", "flip_pct": 1.0, "status": "ok"},
                vol={"iv": 0.30, "rv": 0.20, "trend": None}, tide={"lean": "neutral"})
    assert "chop" in out["headline"].lower() or "pinned" in out["headline"].lower()
    assert out["posture"] == "Stand down"


def test_chop_lifts_to_mixed_when_vol_cheap_and_tide_ok():
    out = _call(gamma={"sign": "POS", "flip_pct": 1.0, "status": "ok"},
                vol={"iv": 0.15, "rv": 0.20, "trend": "falling"}, tide={"lean": "neutral"})
    assert out["posture"] == "Mixed"


def test_event_within_1_day_hard_standdown_overrides_trend():
    out = _call(gamma={"sign": "NEG", "flip_pct": 1.0, "status": "ok"},
                events=[_ev("FOMC Rate Decision", 1, type_="fomc")])
    assert out["posture"] == "Stand down"
    assert out["event"]["severity"] == "veto"
    assert out["event_within_hold"] is True


def test_event_2to5_days_warns_and_downgrades_favorable_to_mixed():
    out = _call(gamma={"sign": "NEG", "flip_pct": 1.0, "status": "ok"},
                events=[_ev("CPI", 3)])
    assert out["event"]["severity"] == "warn"
    assert out["posture"] == "Mixed"
    assert out["event_within_hold"] is True


def test_past_event_today_does_not_veto():
    # event earlier today (negative offset hour) -> already printed -> not a veto
    past = {"event": "CPI", "type": "report",
            "time": (_now() - timedelta(hours=2)).isoformat()}
    out = _call(gamma={"sign": "NEG", "flip_pct": 1.0, "status": "ok"}, events=[past])
    assert out["event_within_hold"] is False
    assert out["posture"] == "Favorable"


def test_low_impact_event_is_ignored():
    out = _call(events=[_ev("Consumer sentiment (final)", 1)])  # not in allowlist
    assert out["event_within_hold"] is False


def test_unknown_gamma_is_conservative_mixed_not_favorable():
    out = _call(gamma={"sign": None, "flip_pct": 0.0, "status": "unavailable"})
    assert out["posture"] in ("Mixed", "Stand down")
    assert "unavailable" in out["headline"].lower()


def test_no_direction_language_anywhere():
    # the header is a REGIME read, never a side call
    for g in ("NEG", "POS"):
        out = _call(gamma={"sign": g, "flip_pct": 1.0, "status": "ok"},
                    events=[_ev("FOMC", 1, type_="fomc")], tide={"lean": "bull"})
        blob = " ".join(str(v) for v in out.values()).lower()
        for banned in (" buy ", " sell ", "calls", "puts", "market up", "market down", "go long", "go short"):
            assert banned not in blob, f"direction language leaked: {banned!r}"
```

- [ ] **Step 2: Run to verify FAIL** — `python -m pytest tests/test_market_regime.py -q` → FAIL (no module).

- [ ] **Step 3: Implement `server/market_regime.py`**

```python
"""Pure market-regime synthesis for the header. NO I/O — callers fetch the
inputs and pass them in. Produces a structured, plain-English regime read that
is a REGIME read (trend vs chop, calm vs fearful, event vs clear) and NEVER a
market-direction call. Posture mirrors the per-ticker verdict at the market
level: Favorable / Mixed / Stand down, willing to stand down on chop days."""
from __future__ import annotations
from datetime import datetime, timezone

_HOLD_DAYS = 5  # weeklies held 1-5d: an event within this window is crossable

# High-impact macro events (the veto allowlist). Matched on type=="fomc" OR an
# event-name keyword. Start conservative.
_HIGH_IMPACT = ("fomc", "cpi", "consumer price", "nonfarm", "payroll",
                "jobs report", "employment situation", "pce", "fed rate",
                "interest rate decision", "ppi")


def _parse_time(s):
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_high_impact(ev: dict) -> bool:
    if (ev.get("type") or "").lower() == "fomc":
        return True
    name = (ev.get("event") or "").lower()
    return any(k in name for k in _HIGH_IMPACT)


def _next_event(events, now):
    """Nearest UPCOMING high-impact event within the hold window. Past events
    (already printed) are excluded — their binary risk is resolved."""
    best = None
    for ev in events or []:
        if not _is_high_impact(ev):
            continue
        t = _parse_time(ev.get("time"))
        if t is None or t <= now:
            continue
        days = (t - now).total_seconds() / 86400.0
        if days > _HOLD_DAYS:
            continue
        if best is None or t < best[0]:
            best = (t, ev, days)
    return best


def compute_market_regime(*, gamma: dict, vol: dict, events: list,
                          tide: dict, opex: bool, now: datetime) -> dict:
    sign = (gamma or {}).get("sign")
    status = (gamma or {}).get("status")

    # 1. Headline — index dealer-gamma regime (plain English, no direction)
    if status != "ok" or sign not in ("POS", "NEG"):
        headline = "Index gamma unavailable — market regime unclear."
        base = "Mixed"
    elif sign == "NEG":
        headline = "Trend regime — moves likely to extend (favorable for directional weeklies)."
        base = "Favorable"
    else:
        headline = "Pinned / chop regime — moves likely to fade (premium decays; hard for weeklies)."
        base = "Stand down"   # chop stands down on its own

    # 2. Event veto (hold-window aware; past events excluded)
    nxt = _next_event(events, now)
    event_within_hold = nxt is not None
    if nxt is None:
        event = {"line": None, "severity": None, "days": None}
    else:
        t, ev, days = nxt
        name = ev.get("event") or (ev.get("type") or "event").upper()
        if days <= 1:
            event = {"line": f"{name} within ~1d — don't initiate weeklies into it.",
                     "severity": "veto", "days": round(days, 1)}
        else:
            event = {"line": f"{name} in ~{int(round(days))}d — any weekly you open now will likely cross it.",
                     "severity": "warn", "days": round(days, 1)}

    # 3. Vol environment, framed for a buyer
    iv, rv, trend = (vol or {}).get("iv"), (vol or {}).get("rv"), (vol or {}).get("trend")
    if iv is None:
        vol_line = "Vol environment unavailable."
        vol_cheap, crush_risk = False, False
    else:
        cheap = (rv is None or iv <= rv) and iv <= 0.22
        vol_cheap = cheap
        crush_risk = (iv > 0.25) and (trend == "falling")
        if cheap:
            vol_line = "Options are cheap to own — calm vol."
        elif crush_risk:
            vol_line = "Vol elevated and falling — IV-crush risk on anything you buy."
        else:
            vol_line = "Vol middling — neither a tailwind nor a clear warning."

    # 4. Tide badge (context, not a direction verdict)
    lean = (tide or {}).get("lean", "neutral")
    tide_badge = {"bull": "tape flow leaning risk-on", "bear": "tape flow leaning risk-off"}.get(
        lean, "tape flow neutral")
    tide_hostile = lean == "bear"

    # 5. Synthesis posture
    if event.get("severity") == "veto":
        posture = "Stand down"
    else:
        posture = base
        if posture == "Stand down" and vol_cheap and not tide_hostile:
            posture = "Mixed"                      # chop liftable to Mixed when cheap+calm+non-hostile
        elif posture == "Favorable" and (crush_risk or tide_hostile or event.get("severity") == "warn"):
            posture = "Mixed"                      # trend downgraded by crush risk / hostile tide / pending event

    return {
        "headline": headline,
        "event": event,
        "vol": vol_line,
        "tide_badge": tide_badge,
        "opex": bool(opex),
        "posture": posture,
        "event_within_hold": event_within_hold,
    }
```

- [ ] **Step 4: Run to verify PASS** — `python -m pytest tests/test_market_regime.py -q` → all pass.
- [ ] **Step 5: Commit**
```bash
git add server/market_regime.py tests/test_market_regime.py
git commit -m "feat(regime): pure compute_market_regime — gamma headline, event veto, posture synthesis"
```
(End every commit body with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.)

---

## Task 2: `fetch_economic_calendar` + contract/probe

**Files:** Modify `server/uw.py`, `server/storage.py`, `tests/contracts.py`, `tests/test_uw_contracts.py`.

- [ ] **Step 1: Failing test** — append to `tests/contracts.py` `CONTRACTS`:
```python
    "economic_calendar": {
        "required": ["event", "time", "type"],
        "note": "market_regime event veto: upcoming high-impact macro events (type incl 'fomc')",
    },
```
And append a live probe to `tests/test_uw_contracts.py`:
```python
@pytest.mark.live
def test_live_economic_calendar_shape():
    import os
    if not os.environ.get("UW_API_KEY"):
        pytest.skip("UW_API_KEY not set")
    from server import uw
    try:
        payload = uw.fetch_economic_calendar()
    except uw.UWError as e:
        pytest.skip(f"econ-calendar live fetch failed: {e}")
    rows = payload.get("data") or []
    if not rows:
        pytest.skip("no econ rows this week")
    from tests.contracts import check_payload
    assert not check_payload("economic_calendar", payload)
    print(f"\n[econ-calendar] {len(rows)} events; types={set(r.get('type') for r in rows[:20])}")
```

- [ ] **Step 2: Run** — `python -m pytest tests/test_uw_contracts.py -k economic -q` → the offline contract test skips if no golden (acceptable); the live probe skips without a key.

- [ ] **Step 3: Implement.** In `server/uw.py` (follow the existing `_get` wrapper pattern):
```python
def fetch_economic_calendar() -> dict:
    """Economic calendar for the current & next week (FOMC/CPI/jobs/etc.).
    Rows: {event, time(ISO), type, forecast, prev, reported_period}."""
    return _get("/api/market/economic-calendar")
```
In `server/storage.py`, add to `_QUASI_STATIC_ENDPOINTS` the entry `"economic_calendar"` (daily-changing → use a ~1h TTL; if the TTL tiers don't have 1h, QUASI_STATIC/24h is acceptable since the loop re-checks and the calendar is stable within a day), and add the wrapper:
```python
def fetch_economic_calendar():
    return _through("economic_calendar", None, None, False,
                    lambda: uw.fetch_economic_calendar())
```

- [ ] **Step 4: Run** — `python -m pytest tests/test_uw_contracts.py -q` → offline passes (live skipped).
  **Live verification (market open + key):** `UW_API_KEY=... python -m pytest tests/test_uw_contracts.py -k economic -m live -s` → confirm `event/time/type` keys + see the event types.

- [ ] **Step 5: Commit**
```bash
git add server/uw.py server/storage.py tests/contracts.py tests/test_uw_contracts.py
git commit -m "feat(regime): fetch_economic_calendar + contract/live probe"
```

---

## Task 3: extend the `Regime` schema

**Files:** Modify `server/schema.py`; Test `tests/test_schema.py`.

- [ ] **Step 1: Failing test** — append to `tests/test_schema.py`:
```python
def test_regime_has_structured_market_fields():
    from server.schema import Regime
    r = Regime(label="normal", headline="Trend regime — …", posture="Favorable")
    assert r.posture == "Favorable"
    assert r.headline.startswith("Trend")
    assert r.event_line is None and r.opex is False        # honest defaults
    # back-compat: existing fields still present
    assert r.vix == 0.0 and r.detail == ""
```

- [ ] **Step 2: Run** — `python -m pytest tests/test_schema.py -k structured_market -q` → FAIL.

- [ ] **Step 3: Implement.** In `server/schema.py`, extend `class Regime` (keep existing `label`, `detail`, `vix`) with:
```python
    headline: str = ""
    posture: Literal["Favorable", "Mixed", "Stand down", ""] = ""
    event_line: str | None = None
    vol_line: str = ""
    tide_badge: str = ""
    opex: bool = False
```
(`Literal` already imported.)

- [ ] **Step 4: Run** — `python -m pytest tests/test_schema.py -q` → pass.
- [ ] **Step 5: Commit**
```bash
git add server/schema.py tests/test_schema.py
git commit -m "feat(regime): structured market-regime fields on Regime"
```

---

## Task 4: macro event feeds the per-ticker cost gate

**Files:** Modify `server/gates.py`; Test `tests/test_gates.py`.

- [ ] **Step 1: Failing tests** — append to `tests/test_gates.py`:
```python
def test_cost_gate_caps_to_red_on_macro_event():
    row = {"ivr": 40, "days_to_earnings": 99}   # would be green on its own
    assert gates._cost_gate(row, event_within_hold=False) == "green"
    assert gates._cost_gate(row, event_within_hold=True) in ("yellow", "red")


def test_cost_gate_default_no_event_unchanged():
    row = {"ivr": 40, "days_to_earnings": 99}
    assert gates._cost_gate(row) == "green"   # default arg = no event
```

- [ ] **Step 2: Run** — `python -m pytest tests/test_gates.py -k cost_gate -q` → FAIL (TypeError: unexpected kwarg).

- [ ] **Step 3: Implement.** In `server/gates.py`, change `_cost_gate` signature + add the macro cap. Current:
```python
def _cost_gate(row: dict) -> Color:
    ivr = row.get("ivr", 100)
    days = row.get("days_to_earnings", 99)
    if days is not None and days < GATE_THRESHOLDS["earnings_days_min"]:
        return "red"
    if ivr <= GATE_THRESHOLDS["ivr_green_max"]:
        return "green"
    if ivr <= GATE_THRESHOLDS["ivr_yellow_max"]:
        return "yellow"
    return "red"
```
Replace with:
```python
def _cost_gate(row: dict, event_within_hold: bool = False) -> Color:
    ivr = row.get("ivr", 100)
    days = row.get("days_to_earnings", 99)
    if days is not None and days < GATE_THRESHOLDS["earnings_days_min"]:
        return "red"
    # Macro event (FOMC/CPI/jobs) inside the hold window — don't buy premium into
    # it, same logic as the per-ticker earnings gate (fed from the market regime).
    if event_within_hold:
        return "red"
    if ivr <= GATE_THRESHOLDS["ivr_green_max"]:
        return "green"
    if ivr <= GATE_THRESHOLDS["ivr_yellow_max"]:
        return "yellow"
    return "red"
```
Update `compute_gates` to pass it through:
```python
def compute_gates(row: dict, history: TickerHistory | None = None,
                  event_within_hold: bool = False) -> dict[str, Color]:
    return {
        "flow":       _flow_gate(row, history),
        "oi":         _oi_gate(row, history),
        "structural": _structural_gate(row),
        "cost":       _cost_gate(row, event_within_hold),
    }
```

- [ ] **Step 4: Run** — `python -m pytest tests/test_gates.py -q` → pass.
- [ ] **Step 5: Commit**
```bash
git add server/gates.py tests/test_gates.py
git commit -m "feat(gates): macro event-within-hold caps the per-ticker cost gate (#7 closed)"
```

---

## Task 5: wire the regime into the snapshot

**Files:** Modify `server/snapshot.py`; Test `tests/test_snapshot.py`.

- [ ] **Step 1: Failing test** — append to `tests/test_snapshot.py`:
```python
async def test_snapshot_attaches_structured_regime(stub_uw, fresh_storage_state, tmp_data_dir):
    snap = await snapshot.refresh_snapshot()
    assert snap.regime.posture in ("Favorable", "Mixed", "Stand down")
    assert snap.regime.headline   # non-empty plain-English headline
```

- [ ] **Step 2: Run** — `python -m pytest tests/test_snapshot.py -k structured_regime -q` → FAIL (regime is the env-based one; posture empty).

- [ ] **Step 3: Implement.** In `server/snapshot.py`:
(a) Add a market-regime builder used by `refresh_snapshot` (and `build_single_row` can reuse the same `Snapshot.regime` — it doesn't rebuild regime). Add:
```python
from server import market_regime

async def _build_market_regime(loop) -> "Regime":
    from datetime import date
    now = datetime.now(tz=timezone.utc)
    spy_spot_raw = await _in_ctx(loop, partial(storage.fetch_stock_state, "SPY"))
    spy_spot = uw.extract_spot(spy_spot_raw) or 0.0
    gmin = round(spy_spot * 0.80) if spy_spot else None
    gmax = round(spy_spot * 1.20) if spy_spot else None
    spy_gex_raw = await _in_ctx(loop, partial(storage.fetch_spot_exposures_strike, "SPY", True, gmin, gmax))
    flip, _wu, _wd, sign, _agg, status = _extract_gex(spy_gex_raw, spy_spot)
    rv_raw = await _in_ctx(loop, partial(storage.fetch_realized_vol, "SPY"))
    iv_raw = await _in_ctx(loop, partial(storage.fetch_interpolated_iv, "SPY", True))
    econ = await _in_ctx(loop, partial(storage.fetch_economic_calendar))
    tide_raw = await _in_ctx(loop, partial(storage.fetch_market_tide))
    reg = market_regime.compute_market_regime(
        gamma={"sign": sign, "flip_pct": flip, "status": status},
        vol={"iv": _regime_iv(iv_raw), "rv": _regime_rv(rv_raw), "trend": None},
        events=_regime_events(econ),
        tide={"lean": _regime_tide_lean(tide_raw)},
        opex=_is_opex_week(now.date()),
        now=now)
    return Regime(label=_env_regime_label(), headline=reg["headline"], posture=reg["posture"],
                  event_line=reg["event"]["line"], vol_line=reg["vol"],
                  tide_badge=reg["tide_badge"], opex=reg["opex"])
```
Add the small helpers `_regime_iv`, `_regime_rv`, `_regime_tide_lean`, `_regime_events`, `_is_opex_week`, `_env_regime_label` (defensive parsers; each returns a safe default on missing data — extract the numeric IV/RV, map tide net to bull/bear/neutral, pass econ `data` rows through, OPEX = the week containing the 3rd Friday, label from the existing `REGIME` env). Keep `compute_market_regime`'s `event_within_hold` available for the row gates: have `_build_market_regime` ALSO return that flag (e.g. return `(Regime, reg["event_within_hold"])`).
(b) In `refresh_snapshot`, replace `regime=_current_regime()` with the computed regime: build it once (`regime_obj, event_flag = await _build_market_regime(loop)`), pass `regime=regime_obj` to the `Snapshot(...)`, and pass `event_flag` down so each `_build_dashboard_row` calls `gates.compute_gates(raw_row, history=None, event_within_hold=event_flag)`. Thread `event_within_hold` as a param of `_build_dashboard_row` (default False so `build_single_row` — which has no market regime in hand — can fetch+compute or default False; simplest: `build_single_row` computes its own regime flag via a lightweight `storage.fetch_economic_calendar()` + the same `_regime_events`/window check, OR passes False with a TODO — pick False default to keep build_single_row unchanged in scope, and note it).
(c) Keep `_current_regime()` as the fallback for `_empty_snapshot` (env-based) so a cold/empty snapshot still has a (degraded) regime.

- [ ] **Step 4: Run** — `python -m pytest tests/test_snapshot.py -q` → pass (the stub provides SPY fetches; econ stub returns its `data`). If the stub lacks `fetch_economic_calendar`/`fetch_stock_state` for SPY, add them to the `stub_uw` fixture returning minimal `{"data":[...]}` so the regime builds.

- [ ] **Step 5: Commit**
```bash
git add server/snapshot.py tests/test_snapshot.py
git commit -m "feat(regime): compute + attach live market regime; event flag feeds row cost gates"
```

---

## Task 6: render the regime header

**Files:** Modify `static/index.html` (`renderRegimeBanner`, ~line 1353); Test `tests/test_html_preservation.py`.

- [ ] **Step 1: Implement.** `renderRegimeBanner` currently reads `snap.regime.label/detail/vix`. Update it to render the structured regime when present (fall back to the old detail string if `posture` is empty, for back-compat). After the existing `const regime = snap.regime || {...}` and the `banner.className`/`label.textContent` lines, set the detail area from the structured fields:
```javascript
  // Structured market regime (live). Falls back to legacy detail when absent.
  if (regime.posture) {
    const postureCls = regime.posture === "Favorable" ? "pos"
                     : regime.posture === "Stand down" ? "neg" : "warn";
    const ev = regime.event_line ? ` · ⚠ ${regime.event_line}` : "";
    const opex = regime.opex ? " · OPEX week (pinning intensifies)" : "";
    label.innerHTML = `<span class="regime-posture ${postureCls}">${regime.posture}</span> — ${regime.headline}`;
    detail.textContent = `${regime.vol_line} · ${regime.tide_badge}${ev}${opex}`;
  } else {
    label.textContent = regime.label === 'risk-off' ? 'Macro risk elevated — fade conviction' : 'Markets normal — green light';
    detail.textContent = regime.detail || `VIX ${(regime.vix || 0).toFixed(1)}`;
  }
```
Add CSS near the regime-banner styles:
```css
  .regime-posture { font-family: var(--ff-mono); font-weight: 700; padding: 1px 7px; border-radius: 4px; margin-right: 4px; }
  .regime-posture.pos { color: var(--ok); border: 1px solid var(--ok); }
  .regime-posture.warn { color: var(--warn); border: 1px solid var(--warn); }
  .regime-posture.neg { color: var(--neg); border: 1px solid var(--neg); }
```

- [ ] **Step 2: Run preservation** — `python -m pytest tests/test_html_preservation.py -q` → pass.
- [ ] **Step 3: Commit**
```bash
git add static/index.html
git commit -m "feat(regime): render live regime header (posture + headline + vol/tide/event/opex)"
```

---

## Task 7: verification

- [ ] **Step 1:** `python -m pytest -q` → all pass (live skipped).
- [ ] **Step 2: Replay smoke** — `$env:DATA_DIR="./data"; $env:REPLAY="1"; uvicorn` → `Invoke-RestMethod /snapshot.json | %{$_.regime}` shows posture/headline (likely "unavailable"/Mixed offline if SPY gex/econ not archived — honest-degrade, expected).
- [ ] **Step 3: Browser** — load, confirm the header shows the posture chip + headline + vol/tide/event line, no number-dump, no direction language. Screenshot.
- [ ] **Step 4: Live verification (market open + key):** run the econ-calendar + SPY-GEX live probes; confirm the headline reads a real regime and the event veto fires correctly.

---

## Self-Review
**Spec coverage:** gamma headline (Task 1/3/6); event veto hold-window + pre/post-print (Task 1 `_next_event`); chop-stands-down (Task 1 synthesis + test); buyer-framed vol (Task 1); tide + OPEX badges (Task 1/6); posture synthesis (Task 1); event→per-ticker cost gate / #7 (Task 4); economic_calendar live + probe (Task 2); SPY-GEX honest-degrade (Task 1 unknown-gamma test; live probe Task 7); no-direction guard (Task 1 test); structured schema (Task 3); render (Task 6). Cross-plan note (#2 thin writer must archive SPY IV/RV+GEX) is in the spec — not code here.
**Placeholder scan:** Task 5 names helper parsers (`_regime_iv` etc.) with explicit behavior described; the implementer writes them defensively — these are small, well-specified, not placeholders. `build_single_row` event flag deliberately defaults False (documented scope choice).
**Type consistency:** `compute_market_regime(*, gamma, vol, events, tide, opex, now) -> dict` with keys headline/event{line,severity,days}/vol/tide_badge/opex/posture/event_within_hold — consistent across Task 1 def, Task 5 caller, Task 6 render. `_cost_gate(row, event_within_hold=False)` and `compute_gates(..., event_within_hold=False)` consistent (Task 4). `Regime` fields (headline/posture/event_line/vol_line/tide_badge/opex) consistent across Task 3/5/6.
