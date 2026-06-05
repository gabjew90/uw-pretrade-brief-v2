# Market Regime Header — Design

**Date:** 2026-06-05
**Scope:** Replace the static, hand-typed regime banner (`REGIME_DETAIL_TEXT` env string — a frozen "VIX 18.4 · FOMC in 6 days" that never updates) with a **live, computed market-regime read** that tells the operator one thing before they scroll to any ticker: *is this a market for owning weekly premium right now?* It is a REGIME read (trend vs chop, calm vs fearful, event vs clear), **never a market-direction call**. Direction stays per-ticker from flow; the header must never imply a side (no recreating the gamma-as-direction mistake at the index level).

**Not in scope:** removing the 60s grid auto-refresh + click-resilience (separate small plan); the skew leg (Plan 3).

## Why
The header is the natural home for the index-level context the per-ticker signal spec deliberately left out. The whole strategy (buying directional weeklies) lives or dies on the market regime: negative dealer gamma = moves extend = weeklies have room; positive gamma = moves fade = premium bleeds while price pins. And the single most useful market-wide "stand down" is a macro event (FOMC/CPI/jobs) — the macro version of the per-ticker earnings gate, which today is buried per-row instead of stated once at the top.

## The header, in plain English (translate every line to "what it means for owning weekly premium" — NO number dumps)

1. **Headline — index dealer-gamma regime** (the most decision-relevant market fact). Compute SPY's `gex_sign` + spot-vs-flip with the EXISTING `_extract_gex` (run the per-ticker logic on SPY).
   - NEG gamma → **"Trend regime — moves likely to extend (favorable for directional weeklies)."**
   - POS gamma → **"Pinned / chop regime — moves likely to fade (premium decays; hard for weeklies)."**
   - unavailable → honest "index gamma unavailable."
   - (QQQ optional as a secondary confirm; SPY is the headline.)

2. **Event veto — market-wide** (hard "stand down" override). `GET /api/market/economic-calendar` (confirmed on-tier; current & next week; `type` includes `fomc`). High-impact events = FOMC / CPI / NFP-jobs / weighty fed-speakers (a small allowlist, start conservative).
   - **Window = the HOLD window, not just tomorrow.** Weeklies are held 1–5 days, so a trade opened today routinely crosses an event 3–4 days out. An UPCOMING high-impact event **within ~5 calendar days** must surface: ≤1 day → strong veto ("FOMC tomorrow — don't initiate weeklies into it"); 2–5 days → warn ("CPI in 3d — any weekly you open now will likely cross it").
   - **Before vs after the print.** Only events still in the FUTURE veto. An event that already printed **earlier today** is binary-risk-resolved (often a clean tape) → NOT a stand-down; drop it from the veto (use the event's date+time vs now). Don't flag the afternoon after a morning CPI.
   - This is the macro earnings-gate; it belongs at the top, not buried per-row — AND it also feeds the per-ticker cost gate (see "Wire the event into the per-ticker cost gate" below).

3. **Vol environment, framed for a BUYER** (not "VIX 14"). SPY **implied vs realized** vol + direction of travel → translate to ownability:
   - cheap & calm (IV ≲ RV, low/steady) → "options are cheap to own — fine."
   - elevated & falling (IV high but declining) → "vol elevated and falling — IV-crush risk on anything you buy."
   - The market-wide instance of the per-ticker IV-rank cost gate. (Sources: SPY `realized_vol` + `interpolated_iv`/`volatility`; "direction of travel" from the archive if available, else level-only with an honest note.)

4. **Light badge — market-tide lean** (`market_tide`): bullish / bearish / neutral tape flow. Context for whether a single-name long is swimming with or against the tape — NOT a direction call for the header's verdict.

5. **Light badge — OPEX week**: pure date calc (third-Friday week). Gamma rolls off → pinning intensifies; flag it.

## Synthesis — one market posture for the strategy
Collapse the above into **Favorable / Mixed / Stand down**, mirroring the per-ticker verdict at the market level:
- **Upcoming event within the hold window is a HARD override → Stand down** (≤1d) / at least the warn-banner downgrades Favorable→Mixed (2–5d), regardless of gamma.
- Else **gamma regime drives the base — and chop is hostile enough to stand down on its own:**
  - NEG / trend → base **Favorable**.
  - POS / pinned-chop → base **Stand down** (premium decays while price pins — premium death for directional weeklies). This is the key calibration: the header must wave you off on a *chop day*, not only an *event day*.
- **Vol + tide are modifiers (can lift OR lower one notch):** from chop-Stand-down, genuinely cheap+calm vol AND a non-hostile tide can lift to **Mixed** (rarely Favorable); from trend-Favorable, IV-crush risk or a strongly hostile tide downgrades to **Mixed**.
- Consequence (intended): the header reads **Mixed or Stand down most days** — including ordinary chop days, not just FOMC days. That's the truth for this strategy, and setting that expectation before the user scrolls is the header's whole job.

## Wire the event into the per-ticker cost gate (not just the header)
The economic-calendar detection computed here must ALSO feed `gates._cost_gate`, not only the banner — otherwise a single name reads all-green while the header says "FOMC tomorrow," the exact "computed but not wired into the verdict" contradiction we keep fixing. Pass an `event_within_hold` (market macro event upcoming within the hold window) into the cost gate so the per-ticker Cost light caps at yellow/red when a macro event is imminent. This closes the separate "event filter is earnings-only" cleanup item (#7) with the SAME data — the per-ticker cost gate currently checks earnings+IV only; it gains the macro-event input here.

## Hard constraints (the framing discipline)
- **No number dump.** Never list raw VIX/GEX/breadth. Every line says what it means for owning weekly premium.
- **Regime read, not a direction call.** "trend vs chop, calm vs fearful, event vs clear" — NEVER "market's up, buy calls." If the header starts implying a side, it has recreated the gamma-as-direction mistake at the index level. (Hard rule, enforced in tests.)
- **Honest-degrade.** Any unavailable input → say so for that line; never fabricate. The synthesis treats unknowns conservatively (lean toward Mixed/Stand down, never fabricate Favorable).

## Architecture
- `server/uw.py`: `fetch_economic_calendar()` → `GET /api/market/economic-calendar`.
- `server/storage.py`: `fetch_economic_calendar()` wrapper (QUASI_STATIC-ish TTL; the calendar changes daily, so ~1h TTL).
- `server/market_regime.py` (NEW, focused module): pure `compute_market_regime(spy_gex, vix_state, econ_events, tide, now) -> dict` producing the structured header (headline text, event line|None, vol line, tide badge, opex flag, posture ∈ Favorable|Mixed|Stand down). Pure + heavily unit-tested (the synthesis + the no-direction rule).
- `server/snapshot.py`: once per snapshot, fetch the market inputs (SPY spot_exposures for gex, SPY realized/iv, market_tide, economic_calendar) and call `compute_market_regime`; attach to the `Snapshot.regime`. ~4 extra calls/cycle (cheap; cached). Thread the regime's `event_within_hold` flag into each row's gate computation.
- `server/gates.py`: `_cost_gate` gains an `event_within_hold` input (the market macro-event flag, from the regime computation, threaded through the row build) — macro event imminent → the per-ticker Cost light caps at yellow/red. Closes the #7 "event filter is earnings-only" cleanup item with the same data.
- `server/schema.py`: extend `Regime` (or add a `MarketRegime` sub-model) with the structured fields (headline, event, vol, tide, opex, posture) — keep `label`/`detail`/`vix` for back-compat or migrate the banner render.
- `static/index.html`: render the regime line(s) from the structured `snapshot.regime` — headline + event veto (prominent when present) + vol + badges + the posture chip. Replace the env-string banner. Retire `REGIME`/`REGIME_DETAIL_TEXT` reliance (keep `REGIME` only as a manual override if desired).

## Testing (TDD)
- `compute_market_regime`: NEG→"Trend regime…favorable"; **POS/chop → posture Stand down on its own** (no event needed); upcoming event ≤1d → Stand down (hard override) regardless of gamma; event 2–5d → warn + downgrade Favorable→Mixed; an event that already printed earlier today does NOT veto; vol/tide lift chop→Mixed only when cheap+calm+non-hostile, and downgrade trend→Mixed on crush-risk/hostile-tide; honest-degrade when inputs missing; **the no-direction guard** — assert the output text never contains buy/sell/calls/puts/"market up/down" (regime vocabulary only).
- `gates._cost_gate`: with `event_within_hold=True`, a per-ticker Cost light that would be green caps to yellow/red — so a row can't read all-green while the header vetoes.
- `fetch_economic_calendar`: contract + golden fixture + opt-in live probe (confirm `type`/date fields).
- **SPY GEX headline input — trust-but-verify** (same as economic-calendar): the headline rests on SPY `spot_exposures`; add a population probe — if it's frequently "unavailable" the headline degrades often, and we need to know.
- Frontend: render from structured regime; honest "unavailable" lines; verify offline via replay + screenshot.

## Honest caveats
- "Direction of travel" for vol needs a prior value (archive). **Cross-plan dependency:** this re-introduces the archive that the deferred #2 (live-on-demand) plan may delete — so the #2 thin daily writer MUST add **SPY IV/RV and SPY GEX** to its endpoint list, or vol-trend and any regime history won't survive going live-on-demand. If absent now, show level-only with an honest note — don't fake a trend.
- The economic-calendar event-importance classification (which events count) needs a small allowlist (FOMC, CPI, NFP/jobs, major fed speakers); start conservative and tune.
- **SPY GEX is a proxy** for "market gamma," which actually concentrates in SPX/0DTE — a good-enough read, not the literal dealer book. Worth a mental asterisk; if a QQQ secondary is added and SPY/QQQ disagree, surface that rather than silently trusting SPY.
