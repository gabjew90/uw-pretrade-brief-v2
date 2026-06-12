# CLAUDE.md additions — §5.5 of the perfect-conjunction directive

Append/merge into the repo's CLAUDE.md. The verdict-semantics block and the
deleted-legs rationale already exist there (four-lights directive 2026-06-12);
what's missing is the PRESENT-stage lock. Add this under the Stage 5 bullet:

---

> **Present semantics (perfect-conjunction overhaul, 2026-06-12): the default
> render is lights-only.** Per ticker, per direction: verdict (`PERFECT` green
> / `NOT NOW — n/N`), the `Waiting on:` line (no_squeeze RED named FIRST — it
> is a veto), ≤5 gate rows (colored lamp + plain-English label, not tappable,
> no numbers on the row), and exactly ONE chart: the session flow strip under
> `smart_flow` (cumulative net opening premium + a dot per qualifying alert,
> radius ∝ premium, anchored "9:30a" / "now · last buy Nm ago" / running $
> total). Exactly four numbers, only at PERFECT or n ≥ N−1: entry toll,
> needs-vs-expects, time stop, contract + max loss.
>
> **DELETED from the default render — do not reintroduce:** flow-timeline,
> regime, and price tiles; provenance badges; per-tile `logic` lines;
> `v-conflicted`; the "How to read this page" paragraph. DARK gates render
> gray with `NO DATA`, counted against n/N — never guessed, never fabricated
> into a chart.
>
> **The "why?" panel is the only disclosure** (one tap opens the whole panel):
> one micro-visual per gate, NONE for `no_squeeze` (categorical — three named
> checks). All visuals share one grammar: **a marker vs a green-shaded
> pass-zone, VALUE-ANCHORED** — real numbers at endpoints, threshold, and
> marker; NO gridlines, NO axis ticks, NO legends. Sub-criterion values vs
> thresholds, provenance, and as_of render as small text under each visual —
> inside the panel only.
>
> **BANNED WORDS (CI-enforced): "Mixed", "Favorable".** Verdict vocabulary is
> `PERFECT` / `NOT NOW — n/N` and nothing else. `tests/test_present_snapshot.py`
> fails the build if they render or if the default-render budget (1 chart,
> ≤4 numerals outside gate rows, ≤5 gate rows, no provenance/logic strings)
> is exceeded.
>
> **Design tokens (frontend):** launch-control annunciator — bg `#14181D`,
> card `#1B2128`, green `#3DD68C`, red `#E5534B`, gray `#5A646F`, amber
> `#E8B33C`; Chakra Petch (verdict/display), IBM Plex Mono (numbers), IBM
> Plex Sans (labels). Responsive: single column ≤900px, auto-fit grid above,
> cards top-aligned. The view-model contract for all of this is
> `Present Contract Extensions.md`; the visual acceptance reference is
> `verdict-mockup.jsx`.

---
