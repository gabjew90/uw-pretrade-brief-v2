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

2. **Event veto — market-wide** (hard "stand down" override). `GET /api/market/economic-calendar` (confirmed on-tier; current & next week; `type` includes `fomc`). If a high-impact event (FOMC / CPI / jobs / fed-speaker of weight) is **today or tomorrow** → top-line **"FOMC tomorrow — don't initiate weeklies into it."** This is the macro earnings-gate; it belongs at the top, not per-row.

3. **Vol environment, framed for a BUYER** (not "VIX 14"). SPY **implied vs realized** vol + direction of travel → translate to ownability:
   - cheap & calm (IV ≲ RV, low/steady) → "options are cheap to own — fine."
   - elevated & falling (IV high but declining) → "vol elevated and falling — IV-crush risk on anything you buy."
   - The market-wide instance of the per-ticker IV-rank cost gate. (Sources: SPY `realized_vol` + `interpolated_iv`/`volatility`; "direction of travel" from the archive if available, else level-only with an honest note.)

4. **Light badge — market-tide lean** (`market_tide`): bullish / bearish / neutral tape flow. Context for whether a single-name long is swimming with or against the tape — NOT a direction call for the header's verdict.

5. **Light badge — OPEX week**: pure date calc (third-Friday week). Gamma rolls off → pinning intensifies; flag it.

## Synthesis — one market posture for the strategy
Collapse the above into **Favorable / Mixed / Stand down**, mirroring the per-ticker verdict at the market level:
- **Event veto is a HARD override → Stand down** (event today/tomorrow), regardless of everything else.
- Else **gamma regime drives the base**: NEG/trend → lean Favorable; POS/chop → lean Mixed.
- **Vol + tide are modifiers**: IV-crush risk or strongly hostile tide can downgrade Favorable→Mixed or Mixed→Stand down.
- It must be willing to read **Mixed or Stand down most days** — that's the truth for this strategy, and the header is the right place to set that expectation before the user scrolls.

## Hard constraints (the framing discipline)
- **No number dump.** Never list raw VIX/GEX/breadth. Every line says what it means for owning weekly premium.
- **Regime read, not a direction call.** "trend vs chop, calm vs fearful, event vs clear" — NEVER "market's up, buy calls." If the header starts implying a side, it has recreated the gamma-as-direction mistake at the index level. (Hard rule, enforced in tests.)
- **Honest-degrade.** Any unavailable input → say so for that line; never fabricate. The synthesis treats unknowns conservatively (lean toward Mixed/Stand down, never fabricate Favorable).

## Architecture
- `server/uw.py`: `fetch_economic_calendar()` → `GET /api/market/economic-calendar`.
- `server/storage.py`: `fetch_economic_calendar()` wrapper (QUASI_STATIC-ish TTL; the calendar changes daily, so ~1h TTL).
- `server/market_regime.py` (NEW, focused module): pure `compute_market_regime(spy_gex, vix_state, econ_events, tide, now) -> dict` producing the structured header (headline text, event line|None, vol line, tide badge, opex flag, posture ∈ Favorable|Mixed|Stand down). Pure + heavily unit-tested (the synthesis + the no-direction rule).
- `server/snapshot.py`: once per snapshot, fetch the market inputs (SPY spot_exposures for gex, SPY realized/iv, market_tide, economic_calendar) and call `compute_market_regime`; attach to the `Snapshot.regime`. ~4 extra calls/cycle (cheap; cached).
- `server/schema.py`: extend `Regime` (or add a `MarketRegime` sub-model) with the structured fields (headline, event, vol, tide, opex, posture) — keep `label`/`detail`/`vix` for back-compat or migrate the banner render.
- `static/index.html`: render the regime line(s) from the structured `snapshot.regime` — headline + event veto (prominent when present) + vol + badges + the posture chip. Replace the env-string banner. Retire `REGIME`/`REGIME_DETAIL_TEXT` reliance (keep `REGIME` only as a manual override if desired).

## Testing (TDD)
- `compute_market_regime`: NEG→"Trend regime…favorable", POS→"Pinned/chop…hard"; event today/tomorrow → posture Stand down (hard override) regardless of gamma; IV-elevated-falling downgrades; honest-degrade when inputs missing; **the no-direction guard** — assert the output text never contains buy/sell/calls/puts/"market up/down" (regime vocabulary only).
- `fetch_economic_calendar`: contract + golden fixture + opt-in live probe (confirm `type`/date fields; same trust-but-verify as other endpoints).
- Frontend: render from structured regime; honest "unavailable" lines; verify offline via replay + screenshot.

## Honest caveats
- "Direction of travel" for vol needs a prior value (archive); if absent, show level-only with a note — don't fake a trend.
- The economic-calendar event-importance classification (which events count as a veto) needs a small allowlist (FOMC, CPI, NFP/jobs, major fed speakers); start conservative and tune.
