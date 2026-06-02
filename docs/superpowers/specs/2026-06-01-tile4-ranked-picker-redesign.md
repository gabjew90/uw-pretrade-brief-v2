# Tile 4 — Contract Picker: Decider → Ranked Comparison Tool (redesign)

**Date:** 2026-06-01
**Scope:** Reshape the Contract Picker from a pass/fail *decider* into a *ranked
comparison tool*. The six factors stay (same data), but they feed a tiered score
that SORTS contracts best→worst instead of vetoing them; each factor is made
self-explaining (shows the actual value + why), and the table leads the layout.
Operator chose: rank-not-decide · all six factors as inputs · thesis→realism→cost
weighting · transparent checks · table-leads-with-slim-top-callout.

## Why

The current build (commit f75f66e) is a decider: Target & Execution are hard-fail
caps that make a contract *ineligible*, one ★ recommendation, losers greyed out,
six cryptic F·C·R·T·E·G dots you must decode. The operator wants to choose from a
ranking, with each check legible on its own terms.

## 1. Scoring model (`server/tile4.py`)

Replace pass/fail eligibility + `pick_best` with a **tiered rank score**. The six
factors keep their current definitions/thresholds but become graded inputs, not
gates. Tiers (lexicographic sort — higher tier dominates):

- **Tier 1 — Thesis** (dominant): Flow + Campaign + Room. Fit with Tiles 1-3.
- **Tier 2 — Realism**: Target (breakeven move vs expected move).
- **Tier 3 — Cost/risk** (tiebreakers): Execution + Greeks.

`rank_contracts(scored) -> list` returns contracts ORDERED best→worst. NO hard
veto: a wide spread / unrealistic target pushes a contract DOWN the order, never
disqualifies it. Sort key = (-tier1_score, -tier2_score, -tier3_score, then the
existing flow→cost→delta tiebreak). The top row is the highlight, not a verdict.

Remove: `eligible`, `pick_best`, `recommendation`, `stand_down`-as-no-pick. KEEP:
`evaluate_gates` exactly as-is (Event / IV-rank / term-structure are still risk
context, shown above the table — a hard gate still flags stand-down, but that's
the GATE layer, independent of the ranking).

## 2. Transparent checks

Each factor on each contract becomes self-explaining: `{ok: bool|null, value,
note}` where `note` is a plain-English string with the actual number + why.
Examples (puts/calls symmetric):
- flow: "smart-money $1.2M at this strike" / "no flow here"
- campaign: "OI building +18% 5d" / "OI flat" / "unknown"
- room: "4.1% to call wall" / "0.3% — at the wall"
- target: "needs 3.2% vs 6% expected" / "needs 9% — unlikely"
- execution: "spread 3%, IV fair" / "spread 40% — bleeds edge"
- greeks: "Δ0.45, θ ok" / "Δ0.08 — too far OTM" / "Δ —" (no greeks)

`score_contract` returns `factors: {flow, campaign, room, target, execution,
greeks}` each a {ok, value, note}, plus `tier1/2/3` subtotals + total. (ok=null
when the underlying data is missing — honest, never a fabricated pass.)

## 3. Display (`renderTile4Picker`, table leads)

- **Gate badges** (Event · IV rank) + **term-structure SVG** — unchanged, on top.
- **Slim top-of-ranking callout**: one line, "Top: NVDA 06/05 760C — δ0.45, ask
  $X, needs 3.2% vs 6%". A highlight, not the prominent verdict card.
- **Ranked table** = centerpiece: rows best→worst, columns: `#` rank, strike,
  ask, Δ, spread, needed-vs-expected, OI, then the six factors shown with their
  transparent value (compact: a ✓/✗/· glyph + the short note on hover/expand, or
  an inline value), and the tier/total score. No greyed-out "demoted" rows —
  everything ranked, nothing hidden.
- Legend explains each of the six factors in one line (what it checks), since the
  point is transparency.
- Stand-down (hard gate tripped) still shows its message in place of the ranking.

## 3b. Basic contract quote per row

Each row also shows the raw chain quote (so you can read it before taking it to a
broker) — already captured in the option-contracts payload, just carry it
through: **bid, ask, last, IV, volume, open interest** (plus mid, and the strike/
type/expiry it already has). These are DISPLAY fields, separate from the scoring
factors. The table groups them as a "quote" cluster (bid/ask/last/IV/vol/OI)
distinct from the "score" cluster (the six factors + tier total), so the
comparison reads cleanly: raw quote on the left, why-it-ranks on the right.

## 4. Backend response shape

`build_tile4` returns `{status, gates, term_curve, expected_move_pct, ranked:
[...], top: <first ranked row or null>}` — `ranked` replaces `contracts` +
`recommendation`. Each ranked row:
`{strike, type, expiry, bid, ask, last, mid, iv, volume, oi,   # raw quote
  premium, delta, theta, spread_pct, be_move_pct,              # derived
  factors:{...}, tier1, tier2, tier3, score, rank}`.
(premium = ask, kept for the breakeven math; bid/ask/last/iv/volume shown raw.)

## Testing (TDD)

- `score_contract`: returns `factors` with {ok,value,note} per factor; tier
  subtotals correct; ok=null on missing data.
- `rank_contracts`: orders by tier1→tier2→tier3; a wide-spread contract ranks
  LOWER but is still present (not dropped); thesis-aligned strike ranks above a
  cheaper non-aligned one.
- Route shape: `ranked` + `top` present; gates unchanged; stand-down still works.
- Frontend: verify OFFLINE via replay (DATA_DIR=./data REPLAY=1) — ranked table
  renders, factor notes legible, top callout present; screenshots. Update
  test_html_preservation if needed.

## Out of scope

Threshold re-tuning (delta band, spread %, IV-rank cutoff — operator did NOT pick
"thresholds"; keep current numbers). Per-trade live re-sort controls (clicking a
column to re-rank). Removing the old Cost tile / Tile 5 (already done).
