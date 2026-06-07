# Tile 1 Greek-Flow Delta Composite — Design (rev 4)

**Status:** spec under review. **Both hard blockers are now CLEARED** with golden-
fixture tests (`tests/test_greek_flow.py` + `server/greek_flow.py` + captured
fixture) — the rest (fetch wiring, render, verdict) is still to build. rev 4: pinned
the sign convention via a known-direction EVENT (no clean-session wait needed), and
the probe reversed the field choice — `total_delta_flow` (tape-consistent) should
lead the headline, `dir_delta_flow` (directional-conviction, diverges from the tape)
becomes the caution lens. **One OPEN DECISION for the operator: confirm that field
choice (see §Field choice).**
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

## Field choice — RESOLVED: `total_delta_flow` leads (operator, 2026-06-06)

Decision: the plain-language headline ("net delta built") is **`total_delta_flow`**
(tape-consistent); **`dir_delta_flow` is the directional-conviction caution lens**,
surfaced via its divergence from the headline/premium. Rationale below.

The 3:32 PM event check (below) surfaced that `dir_delta_flow` and `total_delta_flow`
behave very differently, so which one is the user-facing "net delta built" is a real
choice, not a given:

- **`dir_delta_flow`** = directional-bet bucket. On 6/5 it summed **+5.1M (bullish)**
  and was **+230k at the 19:32 put-sweep minute** — i.e. it diverges from both the
  tape and the price (down day). It's "what the directional/conviction players are
  doing," genuinely informative but it does NOT track the net delta actually traded.
- **`total_delta_flow`** = all options delta. Summed **−96.7M (bearish, matches the
  down day)** and was **−724k at the 19:32 bearish minute (matches the print)**.
  It's the net delta the tape actually built.

For a **novice-readable** "net delta being built" headline, `total_delta_flow` is the
honest, tape-consistent number. `dir_delta_flow` is better as a *secondary*
"directional-conviction lean" lens, where its divergence from total/premium is the
signal. **Recommendation: lead the headline on `total_delta_flow`; surface
`dir_delta_flow` divergence as the caution lens.** (Reverses rev-2's "lead with dir".)
Flag for the operator — this changes what the headline asserts. Either way the sign
convention is shared and pinned (below); thresholds need multi-session calibration,
which is why phase 1 is display-only.

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

- **Headline confidence is CONDITIONAL on agreement** (coherence guard). When delta
  agrees with the opening-$ direction, lead with the plain-language read (novice-
  readable, no jargon): *"net bullish bets ≈ 1.2M shares of upside being bought"* /
  *"net bearish bets ≈ 0.8M shares of downside."* (Map signed delta → "≈ N shares of
  up/downside"; never "Δ"/"delta-weighted".) **When divergence fires, the standalone
  delta headline STANDS DOWN** — don't assert "net bullish bets ≈ 5M" while the rest
  of the tile (puts-lead premium, tide, price, total-delta) reads bearish. Lead
  instead with the disagreement; a lone bullish headline against an all-bearish
  screen is the exact misleading display this tool exists to avoid. (6/5 is the live
  example: opening-$ puts-lead but dir-delta sum +5.1M.)
- **Divergence chip** (when it fires, and it becomes the lead per above):
  *"heads-up: premium leans <X> but the delta flow leans <Y> — they disagree."*
- **Accumulation sparkline**: the **cumsum** curve, with one word — building / fading
  / reversed / choppy.
- **Provisional gate keys off the CLOCK, not row count**: a read is provisional only
  when the session is **live and early** (e.g. < ~60–90 min since the 9:30 ET open) —
  path-efficiency on a half session is low-information ("building at noon can
  round-trip by 3pm"). A complete-but-thin session (half-day/holiday) is NOT
  provisional just because it has few ticks; the closed-state "final" stamp applies.
- Provenance **live**; on a weekend it's the last session's **final** cumsum, frozen
  under the existing "as of <last session>" stamp (per-session; no fetch when closed).

## Hard golden-fixture blockers — BOTH CLEARED (tests/test_greek_flow.py vs the
## captured 6/5 fixture tests/fixtures/uw_greek_flow_SPY.json, 405 rows)

1. **Sign convention — CLEARED via a known-direction EVENT, not a session sum.**
   (Better than the rev-3 "clean session" plan, and weekend-independent.) Anchor:
   19:32 UTC (3:32 PM ET) on 6/5 had ~$6.6M ask-side PUT buying (727P/719P at ask) —
   unambiguously bearish. Assert the net-delta field at that minute is negative →
   pins "negative = bearish, positive = bullish." **Probed result:
   `total_delta_flow` @ 19:32 = −724,802 (negative) ✓ — convention is NOT inverted.**
   IMPORTANT finding: `dir_delta_flow` @ 19:32 = **+230,558 (positive)** — the
   directional bucket netted bullish despite the bearish prints (the $6.6M was ~20%
   of the minute's 50.7k volume). So `dir_delta_flow` ≠ the tape; that drove the
   §Field-choice reversal toward `total_delta_flow` for the headline. Both facts are
   locked as regression tests.
   - *Corroboration available (not required):* `date=` works on greek-flow (probed —
     pulled 06-03), so a clean one-sided session is reachable to double-confirm
     `total_delta_flow` sign; optional follow-up, the event check already stands.
2. **Curve population — CLEARED.** `is_degenerate()` flags empty/flat (all-equal,
   incl. all-zero) → render "unavailable," never a silent permanent "flat." Test
   asserts the 6/5 cumsum has ≥400 rows and actually moves (max≠min). (Same
   silent-death class as the iv/rv key bugs; v1 net_premium=0 fallback in
   unusualwhales.md §3d is the precedent.)

## Budget / provenance

One extra cached per-ticker call (`greek-flow`) on the deep-dive — negligible vs
~15k/day, a few/session.

## Out of scope (phase 1)

- Verdict wiring (modifier) — deferred until thresholds are eyeballed over real
  sessions. If added later: divergence caps, agreement does nothing (per §Confluence).
- Vega flow (`dir_vega_flow`), `total_delta_flow` as a primary read, net-prem-ticks
  $-curve, real-time streaming.
