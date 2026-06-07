# Tile 1 Greek-Flow Delta Composite — Design (rev 2)

**Status:** spec for review (not yet implemented). rev 2 incorporates a live probe
(2026-06-06) that resolved the load-bearing data questions + review feedback.
**Goal:** Add a live "is directional **delta** being *built* this session, and is it
*accumulating* or *round-tripping*?" reading to Tile 1, so the open-state decision
rests on a real-time composite — not premium/side alone.

This is the "#2 addition" from the open/closed-state framing. Tile 1 is the flow
tile, and this is correctly placed there. **It is one flow read seen through a new
lens (delta-weighting), NOT an independent confirming signal — see §Confluence.**

---

## Why (the gap)

Tile 1's live direction = opening call-vs-put **premium** balance. Premium ignores
moneyness/delta — $1M of far-OTM lottos ≠ $1M of 50-delta directional bets.
Delta-weighting fixes that, and the curve shape tells building (conviction) from
round-tripping (churn). Both available on Basic.

## Empirical findings (probed live, SPY, 2026-06-06 — these drive the design)

`/api/stock/{t}/greek-flow` returns **405 per-minute rows**. Fields: `timestamp,
transactions, volume, dir_delta_flow, dir_vega_flow, otm_*, total_delta_flow,
total_vega_flow` (decimal strings).

1. **PER-MINUTE, not cumulative.** Confirmed: `net-prem-ticks.net_delta` (last row,
   −97,231) == `greek-flow.dir_delta_flow` (last row, −97,231) — the same per-minute
   field. So the **daily figure is `sum(...)`** and the **accumulation curve is a
   running `cumsum(...)`**. The raw per-minute series oscillates (±$4.5M) — plotting
   it raw reads as permanent round-trip. *This was the spec's biggest latent bug.*

2. **Sign is entangled with aggregation.** `dir_delta_flow` has mixed per-minute
   signs: `sum = +5,140,917` (bullish) vs `last = −97,231` (bearish) — **opposite**.
   So "sign survives either way" is FALSE here; sum-vs-last and sign-convention are
   one blocker. We use the **cumsum/sum** (the session's net), not the last tick.

3. **Delta-flow is a positioning lens, NOT a price proxy.** That session SPY closed
   **down** (739.07→734.51), yet `sum(dir_delta_flow)=+5.1M` (net-bullish directional
   bets) while `sum(total_delta_flow)=−96.7M` (net-bearish all-in). So:
   - Do **not** validate sign against price direction.
   - Compare the delta sign to Tile 1's opening-$ **flow** direction (flow-vs-flow).
   - `dir_delta_flow` (single-leg directional bets) and `total_delta_flow` (all,
     incl. hedges/spreads) are **different lenses that can oppose** — pick one
     deliberately (see §Field choice), don't average them.

4. **net-prem-ticks adds no independent delta** (its `net_delta` IS `dir_delta_flow`).
   So the core needs ONE endpoint: `greek-flow`. net-prem-ticks' $-premium series is
   optional later context, not required — drops us to one extra call.

## Field choice

Lead with **`dir_delta_flow`** — it's literally "directional delta flow" (single-leg
directional bets), matching "directional delta being built," and excludes the
hedge/spread noise in `total_delta_flow`. Compute `total_delta_flow` too and expose
it only as a secondary "all-positioning" cross-read; when the two oppose (as on
6/5), that's information, not an error. Thresholds need multi-session calibration
before any verdict wiring — phase 1 is display-only precisely to gather that.

## Data wiring

Add `uw.fetch_greek_flow(ticker)` → `/api/stock/{t}/greek-flow` (hyphenated; the path
lint covers it). `fetch_net_prem_ticks` already exists (uw.py) — not needed for
phase 1. Honest-degrade to "unavailable" on failure (no fabricated curve).

## Computation (pure `server/greek_flow.py`, tested vs a golden payload)

1. `cumdelta(rows)` → running cumsum of per-minute `dir_delta_flow` (oldest→newest).
2. `net_delta_built = cumdelta[-1]` (= sum). `sign = bullish|bearish|neutral` with a
   deadband (tiny nets read neutral).
3. `accumulation(cumdelta)` → path read on the cumsum curve:
   - `efficiency = |final| / max(|cumdelta|)` (1.0 = one-way; ≪1 = spiked & reverted).
   - **Zero-crossing aware** (fixes the minor): distinguish *reverted-toward-zero*
     (stayed same side) from *crossed-to-opposite-side* (flipped sign late). Report
     `building | fading | reversed | flat` — not a single round-trip bucket — and
     label the middle (≈0.4–0.7) band "choppy".
4. `composite(opening_direction, delta_sign, accumulation)`:
   - **divergence-veto only** (see §Confluence): returns `diverge` (load-bearing
     caution) when delta sign opposes the opening-$ direction; `building`/`fading`/
     etc. as a descriptive read otherwise. Agreement contributes **zero** toward any
     green. Observed evidence — never follows the operator toggle.

## §Confluence — one read, three lenses (not three signals)

`greek-flow` delta, `net-prem-ticks`, and the opening-$ balance are all signed
options flow — correlated by construction. So the chip must read **"one flow read,
three lenses"**, never "three signals agree." Concretely: **agreement adds nothing**
to conviction (it's the same family confirming itself); only **divergence** is new
information (premium points one way, delta the other). Keep it a pure
divergence-veto so it can never manufacture false multi-signal confidence.

## Render (Tile 1, below the opening-$ balance)

- **Net delta built — plain language, no jargon** (the tool is novice-readable):
  e.g. *"net bullish bets ≈ 1.2M shares of upside being bought today"* /
  *"net bearish bets ≈ 0.8M shares of downside."* Map signed delta → "≈ N shares of
  up/downside." Avoid "Δ", "delta-weighted," etc. in the headline.
- **Accumulation sparkline**: the **cumsum** curve, with one word — building / fading
  / reversed / choppy.
- **Divergence chip** (only when it fires): *"heads-up: the premium leans <X> but the
  delta flow leans <Y> — they disagree."* No chip when they align (no fake confirm).
- **Provisional gate**: early-session reads (under a min elapsed / min ticks, e.g.
  < ~60 min or < N rows) are labeled *provisional* — path-efficiency on a half
  session is low-information; "building at noon can round-trip by 3pm."
- Provenance **live**; on a weekend it's the last session's **final** cumsum, frozen
  under the existing "as of <last session>" stamp (per-session; no fetch when closed).

## Hard golden-fixture blockers (before the read is trusted)

1. **Sign + aggregation**: assert on a captured payload that `sum(dir_delta_flow)`
   has the expected sign for a session of known *flow* lean (cross-checked vs the
   opening-$ direction on the same payload — NOT vs price). The 6/5 data is a usable
   first fixture (down day, dir-delta net +, total-delta net −).
2. **Curve population**: assert the per-minute series is real and the **cumsum is
   non-degenerate** (not all-zero / not flat) — a flat/empty array must render
   "unavailable," never a silent permanent "flat." (Same silent-death class as the
   iv/rv key bugs; the v1 net_premium=0 fallback in unusualwhales.md §3d is the
   cautionary precedent.)

## Budget / provenance

One extra cached per-ticker call (`greek-flow`) on the deep-dive — negligible vs
~15k/day, a few/session.

## Out of scope (phase 1)

- Verdict wiring (modifier) — deferred until thresholds are eyeballed over real
  sessions. If added later: divergence caps, agreement does nothing (per §Confluence).
- Vega flow (`dir_vega_flow`), `total_delta_flow` as a primary read, net-prem-ticks
  $-curve, real-time streaming.
