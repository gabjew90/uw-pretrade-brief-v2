# CLAUDE.md

Project: **UW Pretrade Brief v3** — a clean, properly-bounded rebuild of v2.
Single-user, personal-license, Unusual Whales **Basic-tier**, end-of-day / pre-market
options-decision tool. Same product, rebuilt around typed boundaries.

> v3 keeps v2's *correct* parts (GEX math, research-grounded signal definitions, the
> parquet-archive concept, the budget meter, the degrade scaffolding) and rebuilds the
> *boundaries* that v2 lacked. The product/signal specifics arrive as operator
> instructions; this file locks the **architecture** they must fit.

## The one rule

**The frontend computes nothing.** The server emits a typed *view model* (per element:
a surface value, a detail payload, provenance, and a plain-English label); the client
renders it. This single constraint kills the split-brain bug class (v2's JS gate
hardcodes vs server logic) and forces every upstream separation to fall into place.

## Architecture — a 5-stage pipeline with a typed contract between each stage

Stages never skip a boundary; each consumes the previous stage's typed output only.

1. **Ingest** (`server/pipeline/ingest.py`) → **immutable raw log.** Fetch each UW
   endpoint, store the response *verbatim* with metadata (fetch time, params, tier,
   content hash). Append-only, one file per fetch, atomic temp-write→`os.replace`. No
   transformation on ingest. (Deletes the read-modify-write corruption class; gives
   replay for free.)
2. **Normalize** (`pipeline/normalize.py`) → **canonical typed records.** Parse raw
   into validated pydantic models (`FlowAlert`, `OISnapshot`, `GreekFlowSeries`, …).
   ALL field-shape validation happens here, once — field-drift / sign / truncation
   surprises become explicit failures at this boundary, never silent nulls downstream.
3. **Derive** (`pipeline/derive.py`) → **signals as PURE functions.** direction,
   conviction, dealer-gamma regime, skew, cost, confirmation — each `canonical in →
   signal out`, NO I/O. Golden-fixture + property tests live here. (This is the layer
   that catches sign inversions before they ship.)
4. **Decide** (`pipeline/decide.py`) → **verdict, in exactly one place (server).**
   Consumes signals **by name**, evaluates the gates, emits a verdict + named gate
   results. Because inputs are named, you structurally cannot strand a computed signal
   short of the verdict — an unused signal is a visible unused input.

   > **Verdict semantics (four-lights directive 2026-06-12): strict conjunction, NOT
   > balance-of-evidence.** The question is "are ALL conditions for a perfect long
   > call/put present simultaneously?" Binary answer: `PERFECT` or `NOT NOW — n/N`.
   > FOUR gates (drift): smart_flow, dealer_fuel, cheap_vol, good_entry; puts add the
   > `no_squeeze` hard veto; earnings ≤3d switches to the CATALYST branch
   > (cheap_event, smart_flow, good_entry). Rigor lives in SUB-CRITERIA inside each
   > gate; a measurable fail (RED) trumps an unknown (DARK); DARK counts as not-green
   > but renders gray. **BANNED WORDS: "Mixed", "Favorable"** — no weights, scores or
   > composites; the conjunction IS the model. Thresholds live ONLY in
   > `pipeline/gates.py`, one research citation per constant.
   >
   > **Deleted legs — do not reintroduce** (minimalism: a metric that never flips the
   > decision is noise and must not exist in the product): the **skew leg** (change-vs-
   > baseline is usually DARK at tier history, and the predictability is a borrow-fee
   > artifact — Muravyev-Pearson-Pollet 2022); the **short-volume confirmation**
   > (corroboration that never flips); **conviction as a standalone leg** (same flow
   > family — its only informative case, tape divergence on puts, lives inside
   > no_squeeze); **regime as a cap** (macro routing lives in cheap_vol's clean-window
   > sub-criterion: only a print ≤1 trading day out reds it, operator policy
   > 2026-06-11).
5. **Present** (`pipeline/present.py`) → **view model → dumb frontend.** Per element:
   `{surface, detail, provenance, label}`. The client computes nothing.

   > **Present semantics (data/visual-centric board, operator 2026-06-16 — supersedes
   > the 2026-06-12 lights-only render).** Per ticker, per direction: verdict
   > (`PERFECT` green / `NOT NOW — n/N`), the `Waiting on:` line (no_squeeze RED named
   > FIRST — it is a veto), and ≤5 gate rows. Each gate row is now DATA-FORWARD: a
   > colored lamp + a SHORT label (`gate.short`, 2 words) + the decisive metric
   > (`gate.metric`, e.g. "ask 36% / 70%", "IVR 76 / <30", "θ 22% / 10%") + a compact
   > micro-visual (the wide flow strip for smart_flow, a small one for the rest). The
   > earlier "no numbers on the row / exactly one chart" rule is intentionally REVERSED
   > — the board is meant to read as data + visuals at a glance. Exactly four numbers
   > (the ticket block) still only at PERFECT or n ≥ N−1: entry toll, needs-vs-expects,
   > time stop, contract + max loss.
   >
   > **DELETED from the default render — do not reintroduce:** flow-timeline,
   > regime, and price tiles; provenance badges; per-tile `logic` lines;
   > `v-conflicted`; the "How to read this page" paragraph. DARK gates render
   > gray with `NO DATA`, counted against n/N — never guessed, never fabricated
   > into a chart.
   >
   > **The "why?" panel is the metric TABLE** (one tap): the gate's caption (one line
   > naming the binding reason), then a data grid of every sub-criterion (✓/✗/· · label ·
   > value-vs-threshold), then a provenance footer. The micro-visual itself lives on the
   > gate row now, not in the panel. Visuals share one grammar: **a marker vs a green
   > pass-zone, VALUE-ANCHORED** — real numbers, NO gridlines/axis-ticks/legends.
   >
   > **BANNED WORDS (CI-enforced): "Mixed", "Favorable".** Verdict vocabulary is
   > `PERFECT` / `NOT NOW — n/N` and nothing else. `tests/test_present_snapshot.py`
   > fails the build if they render or if the default-render budget (1 chart,
   > ≤4 numerals outside gate rows, ≤5 gate rows, no provenance/logic strings)
   > is exceeded.
   >
   > **Design tokens (frontend, frozen):** launch-control annunciator — bg
   > `#14181D`, card `#1B2128`, green `#3DD68C`, red `#E5534B`, gray `#5A646F`,
   > amber `#E8B33C`; Chakra Petch (verdict/display), IBM Plex Mono (numbers),
   > IBM Plex Sans (labels). Responsive: single column ≤900px, auto-fit grid
   > above. The view-model contract is
   > `docs/superpowers/specs/2026-06-12-present-contract-extensions.md`; the
   > approved frontend is `static/index-v3.html` + `static/js/` — do not edit it,
   > extend present() instead.

## Cross-cutting services (`server/services/`) — where most of v2's bugs lived

- **Market clock** (`clock.py`) — the single source of truth for trading days,
  holidays, half-days, current session phase, and the **settlement cadence per data
  type** (flow is live intraday; OI settles the next business morning). Every stage
  asks the clock; nothing re-derives from `datetime.now()`. (Kills the
  forming/settled, UTC-vs-ET, weekend-handling, flow-as-"session" bug family.)
- **Provenance** (`provenance.py`) — a **type, not scattered flags**: every value
  carries `source` (live/cache/archive/derived), `as_of`, and `quality`
  (real/degraded/unavailable). Subsumes v2's `is_synthetic` / honest-degrade /
  freshness into one concept that flows through and renders uniformly.
- **Request governor** (`governor.py`) — centralizes the UW budget (120/min, ~15k/day,
  7-day ceiling) as a **priority scheduler**: direction-critical fetches beat
  nice-to-have context; plus request coalescing and graceful degradation surfaced
  *through provenance*.
- **Storage** (`storage.py`) — **append-only parquet + DuckDB read layer.** Three
  tiers: **bronze** (raw), **silver** (canonical), **gold** (signals), as parquet
  partitions queried with DuckDB (SQL over local parquet is the right tool at this
  scale). You **query, never mutate**; compaction is a separate cron. Because bronze
  is immutable, re-deriving gold from bronze = a free backtest harness.

## Domain-model-first

Model **Flow, Positioning, DealerGamma, Skew, Cost, Regime, Verdict** as first-class
entities (`server/models/`); tiles are *views* over them. Adding a signal or
re-skinning for novices never means editing a 1,200-line builder or a 3,800-line HTML.

## Deliberately NOT doing

No microservices, no streaming bus (Kafka), no k8s, no HA/replication, no multi-tenant
layer, no warehouse. One FastAPI process, parquet/DuckDB, cron. Anything more is pure
cost here and would violate the personal-use license. Don't confuse scale with
discipline — spend the budget on correctness, provenance, and iteration speed.

## Tech stack (locked)

Python 3.11 · FastAPI + uvicorn · requests (UW client, 429 backoff) · pydantic v2 ·
pyarrow (parquet write) · **duckdb (read layer)** · google-genai (optional insights) ·
pytest + pytest-mock + responses + pytest-asyncio. UW Basic tier is the binding
constraint. Do NOT add Streamlit / WebSockets / Redis / external DBs / a message bus.

## Keys & secrets

v3 **reuses v2's API keys** — same env var names (`UW_API_KEY`, `GEMINI_API_KEY`,
`BACKFILL_TOKEN`, `RAILWAY_API_TOKEN`, …). The operator's existing keys apply
unchanged; never copy secret *values* into the repo. See `.env.example` for names.

## Data integrity (carried from v2 — these bugs are invisible)

- **UW paths are HYPHENATED** (flow-alerts, spot-exposures, historical-risk-reversal-skew).
  An underscore 404s silently. Lint this at CI.
- Test extractors against **real captured golden payloads** (bronze), asserting a sane
  non-None *value*, not just field presence.
- UW failures honest-degrade — but now that's a **provenance quality tag**, surfaced,
  not a silent null.

## Behavior

- Personal use only. Never claim alpha, edge, or backtested win rates.
- Frequent commits, conventional-commit style. Phone-only operator: paste diffs/links
  into chat; upload binaries to litterbox.
- Offline dev: `REPLAY=1` serves the captured bronze archive, never calls UW.

See `docs/architecture.md` for the full design rationale.
