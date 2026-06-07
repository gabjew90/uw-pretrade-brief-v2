# Tile 1 Greek-Flow Delta Composite — Design

**Status:** spec for review (not yet implemented)
**Goal:** Add a live, directional "is delta being *built* right now, and is it
*accumulating* or *round-tripping*?" reading to Tile 1, so the open-state decision
rests on a real-time composite (opening-$ + vol/OI + ask-side + **greek-flow net
delta** + **net-premium accumulation**) rather than premium/side alone.

This is the "#2 addition" from the open/closed-state framing: Tile 1's live
composite should show *directional delta being accumulated this session*, with the
net-premium tick curve confirming it's piling up one way rather than round-tripping.

---

## Why (the gap today)

Tile 1's live direction = the opening call-vs-put **premium** balance (vol/OI>1,
$-weighted). That's "where the money is pointed." It does **not** show:
- **How much directional delta** the flow has actually built this session (premium
  ignores moneyness/delta — $1M of far-OTM lottos ≠ $1M of 50-delta directional bets).
- Whether that build is **monotonic** (genuine one-way accumulation = conviction)
  or **round-tripping** (opened and closed back = noise/churn).

Both are now available on the Basic tier (probed 2026-06-06).

## Data (probed, Basic tier — real shapes)

**1. `/api/stock/{ticker}/greek-flow`** — per-minute, ~405 rows/session. Fields:
`timestamp, transactions, volume, dir_delta_flow, dir_vega_flow,
otm_dir_delta_flow, otm_dir_vega_flow, otm_total_delta_flow, otm_total_vega_flow,
total_delta_flow, total_vega_flow`. (Decimal strings.)
- `dir_delta_flow` = directional delta flow (signed by aggressor) — the cleanest
  "net delta being built." Positive = net long-delta accumulating (bullish),
  negative = net short-delta (bearish).
- OPEN QUESTION to resolve against a golden payload before coding: are these
  per-interval or cumulative? (`first −13,189 → last −97,231` is ambiguous.) The
  daily figure is then `sum(dir_delta_flow)` (per-interval) or `last` (cumulative).
  Test against a real captured payload — do NOT assume.

**2. `/api/stock/{ticker}/net-prem-ticks`** — per-minute, cumulative through the day
(daily total = last tick; already documented). Fields include `net_call_premium`,
`net_put_premium`, `net_delta`, and ask/bid volume splits. The cumulative
`net_call_premium − net_put_premium` (or `net_delta`) curve is the accumulation
shape: monotone rise = building; rise-then-revert = round-trip.

`net-prem-ticks` already has a wrapper (`uw.fetch_net_prem_ticks`). `greek-flow`
does **not** — add `uw.fetch_greek_flow(ticker)` → `/api/stock/{t}/greek-flow`
(hyphenated; lint covers it).

## Computation (pure, testable against golden payloads)

Add to `server/` a pure module (e.g. `greek_flow.py`) with:

1. **`net_delta_built(greek_flow_rows) -> (value, sign)`** — the session's net
   directional delta (cumulative `dir_delta_flow`, resolved per the open question
   above). `sign = "bullish" | "bearish" | "neutral"` with a deadband so tiny nets
   read neutral.

2. **`accumulation_quality(tick_rows) -> ("building" | "round_trip" | "flat", efficiency)`**
   — path efficiency on the cumulative net-premium curve:
   `efficiency = |final| / max(|cumulative|)` (1.0 = pure one-way; ≪1 = it spiked
   then reverted). Thresholds TBD (e.g. ≥0.7 building, ≤0.4 round-trip).

3. **`delta_composite(direction, net_delta_sign, accumulation) -> read`** — the
   strict-agreement gate the open-state wants: returns "confirms" only when the
   greek-flow delta sign **agrees** with Tile 1's opening-$ `direction` **and**
   accumulation is `building`; "diverges" when delta opposes the premium direction
   (a tell: premium says one side, delta/aggressor says another); else "mixed".

Asymmetric, like skew: divergence is the load-bearing caution (don't act); agreement
is corroboration, not a new green on its own. Net delta is observed evidence — it
does NOT follow the operator toggle (same rule as Tile 2 flow_side).

## Render (Tile 1, below the opening-$ balance)

A compact "delta accumulation" strip:
- **Net delta built**: signed, human units (e.g. "+1.2M Δ built · bullish"), colored.
- **Accumulation sparkline**: the cumulative net-premium curve with a one-word read
  ("building" / "round-tripping" / "flat"). This is the "not appearing out of
  nowhere / not round-tripping" visual.
- **Composite chip**: "delta + premium agree — building" (strict confirm) /
  "delta diverges from premium" (caution) / "mixed".
- Provenance **live**; on a weekend it's the last session's **final** curve, frozen
  under the existing "as of <last session>" stamp (consistent with the open/closed
  states — per-session, no fetch when closed).

## Verdict wiring (optional, phase 2)

The delta composite can feed Positioning as a *modifier* (same shape as Tile 2's
confirmation): agreement+building = mild corroboration; divergence = cap. Keep it a
modifier, never a gate — and decide later; phase 1 is display-only so we can eyeball
it against real sessions first.

## Budget / provenance

Two more per-ticker calls per deep-dive (`greek-flow`, `net-prem-ticks`), cached at
hot TTL — negligible vs ~15k/day, and only on the deep-dive (a few/session). Both
honest-degrade to "unavailable" if a pull fails (no fabricated curve).

## Honesty / tier caveats

- Resolve per-interval-vs-cumulative for `dir_delta_flow` against a **real captured
  payload** (golden fixture), asserting a sane non-None value — the iv/rv-style bug
  class this repo keeps hitting.
- `net-prem-ticks` is cumulative (last = daily total) — don't double-sum it.
- Delta is a probabilistic directional read (aggressor-signed flow), not proof of
  intent — frame it as corroboration, like skew.

## Out of scope (phase 1)

- Vega flow (`dir_vega_flow`) — available on the same call; a later "vol being
  bought/sold" read, not this pass.
- Real-time streaming — steady state stays request-driven/polled.
