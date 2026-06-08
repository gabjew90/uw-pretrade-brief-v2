# Present & ViewModel — Design (v3, Phase 4+)

**Status:** PLAN (awaiting approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** Phase 0 (contracts — `Element`, `ViewModel`, `Verdict`, `Provenance`); Phase 3 (Derive signals); Phase 4+ (Decide verdict)
**Starting point:** `server/pipeline/present.py` (scaffold: stub emits one `Element` per signal + verdict)

## Purpose / role

Build the `ViewModel` that the frontend renders verbatim. The one rule — **the frontend
computes nothing** — is enforced here: every surface value, detail payload, label, tone,
and provenance annotation is a view-model property assembled server-side. The client
iterates `elements[]` and renders; it has no gate logic, no threshold, no signal math.
The novice "glance on the surface, evidence on tap" progressive disclosure is a
**view-model property** (`surface` vs `detail`) built here, not render logic.

## Contract (typed in/out)

```
present(ticker: str, signals: dict[str, Signal], verdict: Verdict,
        *, as_of: str | None) -> ViewModel
```

- **Input:** the full signal map from Derive, the `Verdict` from Decide, the ticker
  string, and `as_of` (ISO timestamp of the oldest value in the pipeline run).
- **Output:** `ViewModel{ticker, as_of, elements: list[Element], verdict: Verdict}`
  where each `Element{key, label, surface, detail, provenance, tone}` is fully
  populated. `surface` is the glance value (one scalar or short string). `detail` is
  the tap payload (structured dict the frontend can render without interpreting).
- **Rule:** `ViewModel` and `Element` are the only types the HTTP response layer
  serialises. No raw `Signal` or `RawRecord` escapes into the response JSON.

## Responsibilities

**Present owns:**
- Mapping each signal to one or more `Element` entries (one signal can produce
  multiple elements — e.g. `flow` → a direction element + a premium element + a
  truncation-warning element).
- Setting `surface`: the glance value — a formatted scalar the user reads at a glance
  (`"CALLS"`, `"$4.2M"`, `"Mixed"`, `"oppose"`, `"block"`). The exact copy / formatting
  is **deferred** to the operator's tile-surface brief; the field structure is fixed now.
- Setting `detail`: the tap payload — a structured dict with the evidence behind the
  surface. Fields and layout deferred to the tile briefs; the pattern is fixed: detail
  is a dict, not a rendered string.
- Setting `tone`: one of `positive | cautionary | negative | neutral | unavailable`.
  Derived from the signal's quality + value (e.g. `positioning=green` → `positive`;
  `skew=oppose` → `cautionary`; `quality=unavailable` → `unavailable`). Tone is the
  only color / sentiment cue the frontend acts on — it never reads signal values to
  decide rendering.
- Setting `provenance` on each `Element` — forwarded from the contributing signal's
  `Provenance` (or `Provenance.worst()` if an element aggregates multiple signals).
- Forwarding `Verdict` verbatim into `ViewModel.verdict` (no re-computation).
- Constructing `ViewModel.as_of` as the worst (oldest) `as_of` across all signals.

**Present does NOT own:**
- Any signal math or gate logic (Derive + Decide only).
- The actual surface copy, label text, or tap layout (operator tile-surface briefs,
  deferred).
- Rendering HTML / CSS / DOM (frontend only).
- Deciding which signals exist (that is Derive's registry).
- Retry / fallback / fetch (Ingest + Normalize only).

## Key behaviors / edge cases

- **`quality=unavailable` signal:** produces an `Element` with `surface=None`,
  `tone="unavailable"`, and `detail={"reason": signal.provenance.note}`. This is
  the uniform "no data" surface pattern — the frontend renders a consistent
  unavailable tile rather than an absent one. Never omit an element for an unavailable
  signal (omission means the tile silently disappears; explicit unavailable is honest).
- **Provenance tint:** `Provenance.source == archive` on any contributing signal sets a
  stale-tint flag in `detail` (or a top-level `ViewModel.stale: bool`). The frontend
  shows a per-tile stale tint for archive-sourced data. Port from v2's per-tile stale
  logic (`e1d6c5e:server/freshness.py` `data_provenance` worst-case); the flag is a
  view-model property here, not a frontend heuristic.
- **Verdict forwarded, not re-derived:** `ViewModel.verdict` is the `Verdict` object
  from Decide. Present does not read `signals["positioning"]` to re-compute the action
  label; it reads `verdict.action` and `verdict.reasons`.
- **`signal_conflict` tone propagation:** when `verdict.signal_conflict == True`,
  elements for conflicting signals (named in `verdict.conflict_legs`) receive
  `tone="cautionary"` regardless of their individual signal value. This makes the
  conflict visually obvious at the element level, not just in the verdict headline.
- **Truncation element:** if the `flow` signal carries `truncated=True` (flow-alerts
  page-cap artifact), present emits an additional `Element{key="flow_truncation",
  tone="cautionary"}` so Tile 1 can surface the warning without the signal math knowing
  about rendering. This is the only case where a derived flag (not a signal gate) drives
  an element.
- **Element ordering:** elements are emitted in a stable, declared order (not sorted by
  dict key iteration). The operator's tile briefs specify the order; the scaffold emits
  alphabetical as a placeholder. Order is a view-model property, not a frontend choice.
- **`detail` is a dict, not a rendered string.** The frontend gets structured data it
  can lay out. A `detail` value that embeds formatted HTML is a violation — present
  must not produce render logic.

## Keepers to port from v2

- **Per-tile `data_provenance` stale flag** from `e1d6c5e:server/freshness.py`:
  `worst_case` of `live / cache / archive` across contributing fields → `ViewModel`
  stale tint. The v2 pattern stamped this on each `Row` field; v3 promotes it to a
  typed `Element.provenance` property.
- **`is_synthetic=True` render-shape flag** from v2's `Row`: this was a render-shape
  flag, NOT a provenance flag (CLAUDE.md). In v3 it is retired — `tone="unavailable"`
  and `provenance.quality=unavailable` replace it uniformly.
- **Tile 1 delta-net composite display** from `e1d6c5e:server/greek_flow.py`
  `build_composite`: the `total_delta_flow` / `dir_delta_flow` / `provisional` shape
  that went into `Row.greek_flow` becomes `Element{key="greek_flow", detail={...}}`.
  Surface and label deferred to the greek-flow-delta operator brief.
- **Freshness `as_of` + `data_provenance`** from `e1d6c5e:server/freshness.py`:
  the contextvar-propagated `FreshnessCollector` pattern. In v3 this collapses into
  `Provenance` on each signal, and `ViewModel.as_of = min(s.provenance.as_of for s in
  signals.values())`. No contextvar needed — provenance flows through the model.
- **Tile 2 `call_is_context` / `put_is_context` flags** from `e1d6c5e:server/schema.py`:
  the "this side had no opening flow, so we show its OI at the OTHER side's strikes"
  distinction. Becomes a `detail` field on the OI element; the frontend renders it
  without knowing what "context" means structurally.

## Acceptance criteria

- [ ] `present(ticker, signals, verdict, as_of=...)` returns a `ViewModel` with one
      `Element` per registered signal (+ verdict element + any warning elements).
- [ ] An `Element` for a signal with `quality=unavailable` has `surface=None`,
      `tone="unavailable"`, `detail` with a `reason` key. It is NOT omitted.
- [ ] `ViewModel.verdict` is the `Verdict` from Decide — not a re-derived copy.
- [ ] `ViewModel.as_of` equals the oldest `as_of` across all signal provenances.
- [ ] When `verdict.signal_conflict=True`, elements named in `conflict_legs` have
      `tone="cautionary"`.
- [ ] `flow_truncation` element is emitted when the flow signal carries `truncated=True`.
- [ ] No `Signal`, `RawRecord`, or `CanonicalModel` appears in the serialised
      `ViewModel` JSON (only `Element` and `Verdict` shapes).
- [ ] `Element.detail` is a dict, never a rendered string or HTML fragment.
- [ ] Archive-sourced signals produce a stale indicator in the ViewModel (via
      `provenance.source == archive` on the contributing element).
- [ ] Element ordering is stable across runs (not dependent on dict insertion order).

## Definition of done (universal)

Typed in/out (`dict[str, Signal]` + `Verdict` in; `ViewModel` out) · provenance: every
`Element` carries `Provenance` forwarded from its contributing signal(s) · no boundary
skipped (no raw data, no bronze/silver read, no gate logic) · REPLAY-reproducible: same
signals + verdict → identical `ViewModel` (pure function).

## Defers to operator

- `surface` copy, `label` text, and `detail` field layout for each element — deferred
  to the tile1/tile2 novice-readability briefs (operator-held). The scaffold emits
  `label = name.replace("_"," ").title()` and `surface = None` until briefs land.
- `tone` derivation rules for each signal value (e.g. at what positioning value does
  tone shift from `positive` to `cautionary`) — deferred to the surface briefs.
- Element ordering per tile — deferred to tile briefs.
- Whether `ViewModel.stale` is a top-level bool or per-element `provenance.source`
  check is sufficient (both are equivalent; operator chooses the rendering contract).

## Open questions / flags

- Should `ViewModel` carry a top-level `regime` element separate from the per-ticker
  signals (regime is market-wide, shared across all tickers in the grid)? Or does
  each ticker's ViewModel embed the same regime element? Current v2 approach: regime
  is in the `Snapshot` header, not per-row. v3 should make this explicit before the
  present stage is wired.
- `Verdict.reasons[]` is a list of plain English strings in the scaffold. Should reasons
  be structured `{signal, category, text}` objects so the frontend can render them with
  consistent styling without parsing strings? Recommendation: yes — but defer the schema
  to the operator's verdict-surface brief. The scaffold uses `list[str]`; upgrade when
  the brief lands.
