# Present Contract Extensions — handoff spec for `present.py`

Companion to the `UW Verdict.html` prototype (PRESENT-stage redesign per
`DIRECTIVE-perfect-conjunction-overhaul.md` §3). The prototype renders fixture
ViewModels in `js/uw-fixtures.js`; this doc is the diff between what
`present.py` emits today and what the redesigned frontend consumes. The one
rule holds: **the frontend computes nothing** — every string below is authored
server-side; the client's only math is pixel geometry.

## 1. Top-level shape (per ticker, per direction)

The current `ViewModel.verdict.{calls,puts}` already carries `direction`,
`branch`, `state`, `green`, `total`, `gates[]`, `waiting_on`. Extensions:

```
DirectionVM {
  ticker: str
  direction: "CALLS" | "PUTS"            # display-cased server-side
  tag: str | None                        # catalyst only, e.g. "EARNINGS THU"
  branch: "drift" | "catalyst"
  state: "PERFECT" | "NOT NOW"           # the only legal headline strings
  green: int, total: int                 # the n/N
  waiting: str | None                    # pre-joined: "Waiting on: cheap options"
                                         # no_squeeze RED named FIRST (veto rule)
  gates: GateVM[]                        # ≤5, server-ordered
  numbers: [label, value][] | None       # exactly ≤4 rows; None unless
                                         # PERFECT or green ≥ total−1 (§3.3)
}
```

`waiting` replaces client-side joins of `waiting_on`; `numbers` reuses the
existing `_numbers()` builder but as labeled pairs:
`["Entry toll","2.6% of ticket"], ["Needs vs expects","+1.1% vs +1.8%"],
["Time stop","3 days"], ["Contract / max loss","$144C 6/26 · $385"]`.

## 2. GateVM

```
GateVM {
  name: str          # smart_flow | dealer_fuel | cheap_vol | good_entry
                     # | no_squeeze | cheap_event
  state: "green" | "red" | "dark"        # lowercase of GateResult.state
  label: str         # plain-English row label (locked vocabulary)
  short: str         # for waiting lines / sheet headers, e.g. "smart money"
  flow: FlowStripVM | None               # smart_flow only — the ONE default chart
  why: WhyVM                             # tap payload, one per gate
}
```

## 3. FlowStripVM — the only default-render chart (§3.2)

NEW — today's `ViewModel.spark` is a bare float list with no alert dots and
no anchor strings. The strip needs:

```
FlowStripVM {
  pts: float[]                 # cumulative net opening premium, sign-adjusted
                               # to the card's direction (reuse spark logic)
  alerts: {i: int, size: float}[]   # one per qualifying flow alert:
                                    # i = index into pts, size ∈ [0,1]
                                    # normalized premium (dot radius ∝ premium)
  total: str                   # running $ total at line end, "$4.2M"
  startNote: str               # "9:30a"
  endNote: str                 # "now · last buy 22m ago" — server computes
                               # the minutes from the clock service
}
```

Colored client-side by `gate.state` only. The still-building shaded zone is
drawn at ≥90% of the series max (the gates.py constant rendered, not re-derived
— if the threshold changes, emit `buildFrac: 0.9` alongside).

## 4. WhyVM — one micro-visual per gate, shared grammar

All visuals are marker-vs-green-pass-zone, value-anchored: the view model
supplies BOTH the raw numerics (for marker geometry) and every display string
(endpoints, threshold, marker). No client formatting.

```
WhyVM {
  kind: "tug" | "ladder" | "cheap_vol" | "runway" | "dot_strip" | None
  caption: str | None          # one line, the gate's own plain English
  subtext: str | None          # sub-criterion values vs thresholds +
                               # provenance + as_of — ONLY rendered inside
                               # the expanded panel (never on default)
  data: {...} | None           # per-kind, below
  items: [bool|None, str][]    # no_squeeze only: three named checks,
                               # None = no data (renders ·, counted not-green)
  missing: str[]               # DARK chart-gates: the absent inputs, verbatim
                               # provenance notes — no visual is drawn
}
```

Per-kind `data` (all label strings server-formatted):

- `tug` (smart_flow): `leftPct, leftLabel ("$4.2M calls (82%)"),
  rightLabel ("$0.9M puts"), threshPct (70), threshLabel ("70% needed")`
- `ladder` (dealer_fuel): `spot, flip, wall` (numeric, geometry) +
  `spotLabel "$142.10", spotNote "you are here", flipLabel, flipNote
  "fuel off below", wallLabel, wallNote "ceiling", roomLabel
  "1.4 expected moves of room"`
- `cheap_vol`: `actual, charged, ivRank` (numeric) + `actualTitle/Label,
  chargedTitle/Label, rankTitle, rankLabel "22/100", leftAnchor "cheapest ←",
  rightAnchor "→ priciest"` (green pass segment is rank 0–30; emit
  `rankPassMax: 30` if the gates.py constant ever moves)
- `runway` (good_entry): `needPct, expectPct, tollPct, passFrac (0.7)` +
  `needLabel "+1.1%", needNote "break even", zeroLabel "0%",
  expectLabel "+1.8% expected"`
- `dot_strip` (cheap_event): `moves[8], implied, avg` (numeric) +
  `impliedLabel "±6.1%", impliedNote "price of this report",
  avgLabel "avg 8.9%", dotsNote "● last 8 report moves"`
- `no_squeeze`: no kind, no chart — `items` only (deliberate, §3.4)

## 5. Landing grid (`/api/grid`)

Endpoint mapping the prototype's data layer (`js/uw-data.js`) already calls
in "live api" mode:

- `GET /api/grid` → `GridVM` (below)
- `GET /api/view/<ticker>` → `{ ticker, best: "calls"|"puts",
  calls: DirectionVM, puts: DirectionVM }`

The prototype is served same-origin (drop `UW Verdict.html` + `js/` into
`static/`), or cross-origin for dev via `?api=http://host:port`.

```
GridVM {
  asOf: str                    # "12:42 PT"
  status: str                  # "Swept 18 names · refreshed 12:42 PT · next sweep 13:00"
  rows: {ticker, direction, state, green, total, tag}[]
                               # best direction per ticker, sorted n desc,
                               # PERFECT pinned top — SERVER-sorted
}
```

Plus two non-grid states the scanner endpoint should be able to emit
(the empty screen says what the scanner is doing — never a blank list):

```
ScanningVM { asOf, headline, progress ("7 of 18 names checked"),
             detail, note }
EmptyVM    { asOf, headline ("NOT NOW — ALL 18 NAMES"), body,
             closest: {label, ticker}, next }
```

## 6. Deleted from the default render (§3, do not re-emit for it)

flow-timeline tile · regime tile · price/candles tile · provenance badges ·
per-element `logic` lines · `v-conflicted` · "How to read this page".
Provenance survives ONLY as `why.subtext` (expanded panel) and as DARK gates
(gray, NO DATA, counted against n/N).

## 7. Snapshot tests (§5.4) — mirrored in the prototype

`js/uw-contract-tests.js` runs the §5.4 assertions against the live DOM
(readout in the Tweaks panel): banned words ("Mixed"/"Favorable") never render;
verdict vocabulary ∈ {PERFECT, NOT NOW}; collapsed cards have ≤1 svg and it is
the flow strip; ≤5 gate rows; ≤4 number rows; no provenance/logic on default;
expanded cards have one micro-visual per gate, zero svg for no_squeeze; no
legends or axis ticks anywhere. Port these one-to-one into
`tests/test_present_snapshot.py` against the rendered template.

## 8. Open items for the pipeline

- `alerts[].size` needs per-alert premium retained through derive → present
  (today only `top_alerts` strings survive).
- `endNote` minutes-since-last-print needs the clock service, not `now()`.
- Cross-sectional phrases ("top 4% of the whole market today") come from the
  derive-stage panel percentiles (§2.5 of the directive).
- `green ≥ total−1` numbers rule is decided in present, not the client.
