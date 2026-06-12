# Brief for Claude Code — wire the PRESENT redesign (server side)

Repo: uw-pretrade-brief-v2. Drop this folder's contents in at the repo root
first (`UW Verdict.html` + `js/` are the approved frontend; `handoff/` and
`Present Contract Extensions.md` are your contract).

You are implementing the server side of the PRESENT-stage redesign. The
frontend is DONE and frozen — do not edit `UW Verdict.html` or `js/*` except
to move them into `static/`. Your job is to make `present.py` emit what they
consume.

Authoritative references, in priority order:
1. DIRECTIVE-perfect-conjunction-overhaul.md — §3 (present) and §5.4 (tests).
2. Present Contract Extensions.md — the exact view-model shapes the frontend
   renders (DirectionVM / GateVM / WhyVM / FlowStripVM / GridVM). The
   prototype fixtures in js/uw-fixtures.js are worked examples of every shape
   and every verdict state — treat them as golden output to imitate.

Work order:
1. Port handoff/test_present_snapshot.py into tests/, wire its
   `all_viewmodels` fixture to present() over the REPLAY bronze archive plus
   synthetic gate fixtures covering: PERFECT, NOT NOW with one RED, catalyst
   branch with tag, a DARK gate, and a no_squeeze RED veto on puts. Tests
   first — they must fail before present.py changes and pass after.
2. Extend present() to emit the contract: per-direction {state, green, total,
   waiting (pre-joined, no_squeeze RED named FIRST), tag, gates[], numbers
   (≤4 pairs, only at PERFECT or green ≥ total−1)}. Per gate: {name, state
   (lowercase), label, short, flow (smart_flow only), why}. All strings
   formatted server-side — the frontend computes nothing.
3. FlowStripVM: pts from the existing spark cumsum; alerts[] needs per-alert
   premium retained through derive (size normalized 0–1); endNote minutes
   from the clock service, never datetime.now().
4. Why payloads per kind (tug / ladder / cheap_vol / runway / dot_strip):
   raw numerics for geometry PLUS display strings for every anchor, threshold
   and marker, exactly as in the spec §4. no_squeeze: items[] only, no kind.
   DARK gates: missing[] from provenance notes, no data, no fabrication.
5. /api/grid → GridVM (best direction per ticker, sorted n desc, PERFECT
   pinned top, server-sorted) plus the scanning/empty payloads (spec §5).
6. Move UW Verdict.html + js/ into static/ (keep fixtures — they power demo
   mode and the in-page §5.4 readout). Re-capture the golden ViewModel via
   scripts/capture_golden_vm.py.
7. Update CLAUDE.md with handoff/"CLAUDE.md additions.md" (§5.5).

Hard rules (non-negotiable):
- "Mixed" and "Favorable" are banned strings; the snapshot test fails if they
  appear anywhere in an emitted view model.
- Verdict vocabulary: PERFECT / NOT NOW — n/N only.
- DARK counts against n/N, renders gray NO DATA — never guessed.
- Thresholds live ONLY in pipeline/gates.py; if a constant feeds a visual
  (70% dominance, IV-rank 30, still-building 90%, runway 70%), emit it in the
  view model rather than letting the frontend hardcode it drifting.
- No client-side computation: if a visual needs a value the ViewModel lacks,
  extend present(), never the JS.
- UW paths are HYPHENATED; REPLAY=1 must produce byte-identical view models.

Out of scope: the frontend's look, the gate logic itself (decide/derive
already emit GateResults — you are formatting, not re-deciding), websockets,
new endpoints beyond /api/grid's reshape.
