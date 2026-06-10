# Frontend — Design (v3, Phase 5)

> **AS-BUILT (2026-06-10).** Shipped in `static/index.html`; deltas:
> - The "multi-ticker view" flagged below as out-of-scope SHIPPED: `/api/grid` hot-ticker
>   landing (one cross-ticker call), click-through to `?t=<ticker>`, back link, go-to box.
> - Header = THE CALL block (labelled, tone-colored verdict + reasons + verdict_logic);
>   the market context renders as a normal tile.
> - `detail` is rendered as key/value rows (it is a dict, resolving the open question).
> - `element.series` arrives ({kind, points[]}) and is currently IGNORED by this renderer
>   — it is the contract the future chart UI draws from, computing nothing.
> - The CI gate exists: `tests/test_no_client_compute.py` (banned patterns + no threshold
>   constants); REPLAY parity is gated by `tests/test_replay_parity.py` against a
>   committed golden ViewModel (re-capture via `scripts/capture_golden_vm.py`).

**Status:** AS-BUILT (was PLAN) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** Phase 3 (a ViewModel exists at `/api/view/<ticker>`) · **Starting point:** `static/index.html`

## Purpose / role

Render the server's `ViewModel` verbatim on a phone-sized dark terminal surface.
No signal logic. No decision logic. No structural computation. The value of the
frontend is entirely in its fidelity to the ViewModel contract — a correct render
of the wrong shape is still a bug; a correct render of the right shape is the
whole job.

## Contract (consumes ViewModel/Element verbatim)

As declared in `docs/superpowers/specs/2026-06-08-contracts-and-models-design.md`:

```
ViewModel { ticker, as_of, elements: Element[], verdict: Verdict }
Element   { key, label, surface, detail, provenance, tone }
Provenance{ source: live|cache|archive|derived,
            quality: real|degraded|unavailable, as_of, note }
Verdict   { action, reasons[], signals_used[], provenance }
```

The frontend consumes this shape at `/api/view/<ticker>` and renders each field
where it appears. It does not interpret `surface`, does not re-derive `tone`, and
does not infer quality from any value it reads — it maps `provenance.quality →
CSS class` and stops.

## Responsibilities (and explicit NON-responsibilities)

**Responsibilities:**
- Fetch `/api/view/<ticker>` on load and on explicit user refresh.
- Render `Element[]` in server-declared order, surface first, detail on tap.
- Map `provenance.quality` → visual tint class (one-to-one, no logic).
- Display `ViewModel.verdict.action` and `ViewModel.as_of` in the header bar.
- Show a degraded/unavailable placeholder (`—`) when `surface` is null.
- Accept `?t=<ticker>` query param; accept manual ticker entry that triggers a
  new fetch (no server-side orchestration in JS — just a new fetch to the same
  endpoint with the new ticker).

**Explicit NON-responsibilities (the one rule):**
- No gate thresholds, no signal math, no direction derivation, no score
  computation of any kind.
- No session/clock logic (no `new Date()` to decide "market is open" — that
  answer comes from `provenance.source` and `ViewModel.as_of`).
- No tone computation — `element.tone` arrives from the server; JS only maps it
  to a color token.
- No layout decisions driven by signal content — tile order is server-declared
  via `elements[]` order.
- Axis ticks, pixel geometry, and locale number formatting (`Intl.NumberFormat`)
  are the only permitted client math — presentation, not derivation.

## Component tree / rendering model

```
App
├── Header          ← vm.ticker · vm.as_of · vm.verdict.action · ViewModel.provenance
├── ElementList     ← vm.elements (in order)
│   └── ElementCard (per element)
│       ├── SurfaceRow   ← e.label · e.surface · tint(e.provenance.quality)
│       └── DetailDrawer ← e.detail (tap to expand; rendered verbatim as text/pre)
└── VerdictPanel    ← vm.verdict.action · vm.verdict.reasons[] · vm.verdict.provenance
```

Each `ElementCard` is a single `<div class="el q-{quality}">`. Tap toggles the
`DetailDrawer` open; detail payload is displayed as-received (no parsing, no
reformatting beyond newline preservation). The `VerdictPanel` renders
`verdict.action` as a headline and `reasons[]` as a bulleted list — strings
verbatim from the server.

## Progressive disclosure as a view-model property

Surface vs. detail is not a frontend decision. `element.surface` is what the
glance shows; `element.detail` is what the tap reveals. The frontend's job is:

1. Render `surface` always visible.
2. On tap, reveal `detail` in-place.
3. On second tap (or tap-away), collapse.

The novice-readability copy inside `surface` and `detail` is authored server-side
by the present layer, sourced from operator briefs. The frontend renders whatever
string arrives — it does not rewrite, summarize, or abbreviate.

## Provenance rendering

Quality maps one-to-one to a left-border tint (already in the scaffold):

| `quality`     | CSS class        | Visual           |
|---------------|------------------|------------------|
| `real`        | (none / default) | no tint          |
| `degraded`    | `q-degraded`     | `--warn` amber   |
| `unavailable` | `q-unavailable`  | `--axis` muted + 0.6 opacity |

The `provenance.note` string, if present, is appended to the `DetailDrawer` as a
dim footer — never surfaced on the glance row. `provenance.as_of` feeds the
per-element staleness note when the operator brief specifies one (string from
server, rendered verbatim).

No quality is inferred from value content. `q-unavailable` is not triggered by a
null `surface` unless `provenance.quality == "unavailable"`.

## Theme

Carry the existing dark-terminal CSS custom properties exactly as declared in the
scaffold:

```css
--bg: #0b0d12; --panel: #12151c; --grid: #232836; --text: #d7dbe6;
--text-dim: #8b93a7; --axis: #5a6378;
--ok: #9ECE6A; --neg: #F7768E; --warn: #E0AF68;
--ff-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
```

`--ok`/`--neg` map to `tone` values supplied by the server (e.g., `tone: "ok"` →
`color: var(--ok)`). No additional color tokens without a corresponding server
`tone` value to drive them.

## Acceptance criteria

- [ ] Fetches `/api/view/<ticker>` and renders `elements[]` in server order with no
      client reordering.
- [ ] `provenance.quality` → CSS class mapping is a one-to-one lookup with no
      conditional logic on value content.
- [ ] Tap on any `ElementCard` reveals `element.detail`; tap again or outside collapses.
- [ ] `VerdictPanel` renders `verdict.action` and `verdict.reasons[]` as received
      (no string manipulation).
- [ ] `surface == null` renders a placeholder `—`; does NOT trigger `q-unavailable`
      unless `provenance.quality == "unavailable"`.
- [ ] No `new Date()`, no threshold constants, no gate expressions anywhere in JS
      (CI grep asserts: `new Date|threshold|>.*prem|<.*prem|gate|score` fails if
      found outside comments).
- [ ] REPLAY parity: loading the page with `REPLAY=1` and the same ticker yields
      identical render (same elements, same tints, same verdict) as live with identical
      bronze.
- [ ] Renders usably on a 390px-wide viewport (phone-only operator).

## Definition of done (universal)

- Renders ViewModel verbatim — no field transformed, summarized, or re-derived.
- Provenance tint rendered consistently on every element, including `null` surface.
- REPLAY-reproducible: same bronze → same ViewModel → identical render.
- CI rejects client computation: grep for threshold constants + `new Date` in JS
  exits non-zero if any match outside comments.

## Defers to operator

All novice-readability copy (the words inside `element.label`, `element.surface`,
`element.detail`) and all tile layout decisions (which elements appear, in what
grouping, with what section headers) are authored in `present.py` per the
operator's tile-1/tile-2 novice-readability briefs. Those briefs are
operator-held and are NOT in the repo. The frontend renders whatever the server
emits — it does not invent or constrain copy.

## Open questions / flags

- **Ticker entry UX:** simple `<input>` + submit on Enter is assumed. If the
  operator brief specifies a ticker-picker or search-as-you-type, that is a layout
  addition not a logic addition — still just a new fetch on confirm.
- **VerdictPanel position:** above or below `ElementList`? Operator brief decides.
  Default assumption: above (headline first, details below).
- **Detail payload shape:** currently typed as `any` in the contract. If `detail`
  is a structured object (dict) rather than a plain string, the frontend will need
  a verbatim JSON renderer (key–value rows, no interpretation). Resolve when
  `present.py` emits its first non-string detail.
- **Multi-ticker view:** not in scope for Phase 5. A grid/list of tickers would
  be a second endpoint (`/api/grid`) + a second render path — declare separately
  when the operator brief calls for it.
