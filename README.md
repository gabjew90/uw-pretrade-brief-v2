# UW Pretrade Brief

An end-of-day / pre-market options-decision tool for naked weekly calls and puts. It
reads Unusual Whales data through a typed pipeline and answers one question per ticker,
per direction:

> **Are *all* the conditions for a good long-premium trade present right now?**

The answer is binary on purpose — **PERFECT** or **NOT NOW — n/N** — with the failing
gates named. No scores, no weighted composites, no "on balance." The conjunction *is*
the model.

**▶ Live demo:** https://uw-pretrade-brief-v2-production.up.railway.app/

<!-- Drop a screenshot at docs/img/hero.png and uncomment:
![UW Pretrade Brief — verdict cards](docs/img/hero.png)
-->

---

## What it does

For every hot ticker the scanner surfaces, the server evaluates **calls and puts
independently** against a small set of gates and renders a launch-control board: one
lamp per gate (green / red / gray), one plain-English "waiting on…" line, the session
flow strip, and — only when a trade is one gate away — the four numbers that matter
(spread, breakeven vs. expected move, time stop, the contract and its max loss). Every
gate opens a "why?" panel with a value-anchored micro-visual.

The verdict has two branches, chosen by the calendar:

- **DRIFT** (no earnings in the hold window) — `smart_flow`, `dealer_fuel`,
  `cheap_vol`, `good_entry`; puts add the `no_squeeze` veto.
- **CATALYST** (earnings ≤ 3 trading days out) — `cheap_event`, `smart_flow`,
  `good_entry`.

Each gate is a conjunction of sub-criteria, and each threshold is grounded in published
research, cited in [`server/pipeline/gates.py`](server/pipeline/gates.py):

| Gate | Asks | Grounding |
|------|------|-----------|
| `smart_flow` | Did opening, ask-side premium *dominate* this side — and is it still building intraday? | Hu 2014 (opening ask-side imbalance) |
| `dealer_fuel` | Will dealer hedging *amplify* the move (negative gamma, spot past the flip, room to a wall)? | Barbon-Buraschi 2020 |
| `cheap_vol` | Are the options cheap for the movement (low IV rank, realized ≥ implied, flat/upward term)? | Hu-Jacobs 2020, Goyal-Saretto, Vasquez 2017 |
| `good_entry` | Is the spread / breakeven / theta / delta survivable? | spread-vs-move risk math |
| `no_squeeze` *(puts)* | Are you walking into a short squeeze (FTDs, IV spike, tape pushing the wrong way)? | Muravyev-Pearson-Pollet 2022 |
| `cheap_event` *(catalyst)* | Is the implied move cheap vs. the stock's own earnings history? | Milian 2023 |

Hold guidance leans 1–3 days with a time stop because excess short-horizon momentum
reverts (Baltussen et al.); the catalyst branch exits on the report day before the IV
crush (de Silva et al. 2024). The skew leg was *deleted* on purpose — its predictability
is a borrow-fee artifact (Muravyev-Pearson-Pollet) and it was usually unreadable at this
data tier. A metric that never flips the decision is noise.

---

## Architecture

A 5-stage pipeline with a **typed contract between every stage**. Stages never skip a
boundary; each consumes only the previous stage's typed output.

```
ingest → normalize → derive → decide → present
(raw)    (canonical)  (signals) (verdict) (view model → dumb frontend)
```

1. **Ingest** — fetch each UW endpoint, store the response *verbatim* (append-only, one
   file per fetch, atomic temp-write → `os.replace`). Immutable raw log; replay for free.
2. **Normalize** — parse raw into validated pydantic models. *All* field-shape validation
   happens here, once — drift/sign/truncation surprises become explicit failures at the
   boundary, never silent nulls downstream.
3. **Derive** — signals as **pure functions** (`canonical in → signal out`, no I/O).
   Golden-fixture + property tests live here; this is where sign inversions get caught.
4. **Decide** — the verdict, in exactly **one place**. Consumes signals by name, runs the
   gates, emits `PERFECT` / `NOT NOW — n/N` + the failing gates.
5. **Present** — a typed view model the client renders verbatim.

### The one rule: the frontend computes nothing

The server emits, per element, a surface value + plain-English meaning + provenance +
pre-formatted strings. The browser maps fields to pixels and does nothing else — no
thresholds, no direction math, no clock. This kills the entire split-brain bug class
(client recompute drifting from server logic) and is **enforced in CI** by
[`tests/test_no_client_compute.py`](tests/test_no_client_compute.py), which greps the
shipped frontend for banned arithmetic and constants.

### Cross-cutting services — where v2's bugs lived

- **Clock** ([`services/clock.py`](server/services/clock.py)) — single source of truth
  for trading days, half-days, session phase, and *settlement cadence per data type*
  (flow is live intraday; OI settles next morning). Nothing re-derives from `now()`.
- **Provenance** ([`services/provenance.py`](server/services/provenance.py)) — a **type,
  not scattered flags**: every value carries `source` (live/cache/archive/derived),
  `as_of`, and `quality` (real/degraded/unavailable). Honest-degrade is a first-class
  state: an unreadable gate renders gray **DARK**, counts as not-green, and is *never*
  fabricated into a value or a chart.
- **Governor** ([`services/governor.py`](server/services/governor.py)) — centralizes the
  UW budget as a priority scheduler; direction-critical fetches beat nice-to-have
  context; degradation surfaces through provenance.
- **Storage** ([`services/storage.py`](server/services/storage.py)) — append-only
  parquet in three tiers (**bronze** raw / **silver** canonical / **gold** signals),
  queried with **DuckDB**. You query, never mutate. Because bronze is immutable,
  re-deriving gold from bronze is a **free backtest harness** (see below).

---

## Replay = backtest, for free

`REPLAY=1` serves the captured bronze archive and never calls UW — the governor denies
live calls and ingest reads the latest bronze. No market, no key, no rate limits. Same
mode powers [`scripts/backtest_replay.py`](scripts/backtest_replay.py): because derive
and decide are pure and bronze is immutable, replaying each archived session through the
*current* code reconstructs what every gate would have said that day, and joins the next
session's move to score it. It also emits the **gate-binding histogram** — which gate
blocks PERFECT most often — so thresholds get tuned from data, not vibes. (A test fails
the build if PERFECT ever fires on a meaningful fraction of ticker-days; rare-by-design
is the thesis, daily-firing means a threshold is wrong.)

---

## Setup

This is a [uv](https://docs.astral.sh/uv/) project (the `uv.lock` is the source of
truth; the app runs from the repo root — `server` is not installed as a package, by
design).

```bash
uv sync                                  # installs deps incl. the dev group from uv.lock
cp .env.example .env                      # add your UW key (Basic tier or higher)
uv run uvicorn server.main:app --port 8000
```

Plain-pip fallback (no uv):

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
.venv/bin/python -m uvicorn server.main:app --port 8000
```

Open http://localhost:8000. **No live key?** Prefix with `REPLAY=1` to serve the
captured archive offline — no market, no key, no rate limits.

The frontend is React over a CDN with no build step (Babel-standalone in the browser) —
nothing to compile.

## Tests

```bash
uv run pytest -q          # 261 passing
```

The suite is the spec: golden-fixture parsing against real captured payloads, pure-
function property tests for every signal, the locked decide combination structure, a
REPLAY parity gate (byte-identical view model from committed golden bronze), the
frontend-computes-nothing grep, and the backtest reachability guard.

## Tech stack

Python 3.11 · FastAPI + uvicorn · pydantic v2 · requests (UW client, 429 backoff) ·
pyarrow + DuckDB · React (CDN, no build) · pytest. Deployed as one process on Railway
with an in-app nightly maintenance job (backup / compact / backtest). No microservices,
no message bus, no external DB — deliberately. UW **Basic tier** is the binding
constraint, so the spend goes to correctness and provenance, not scale.

## How it was built

Spec-driven and AI-orchestrated: one design spec per subsystem in
[`docs/superpowers/specs/`](docs/superpowers/specs/), the locked architecture in
[`CLAUDE.md`](CLAUDE.md), the full rationale in
[`docs/architecture.md`](docs/architecture.md). Every data-integrity decision traces to
a captured payload or a cited paper.

## Scope & disclaimer

Personal-use project on a personal UW license. It informs a discretionary trader's own
decisions — it is **not financial advice**, makes **no claim of edge, alpha, or
backtested win rates**, and never re-serves UW data. The "verdict" is a disciplined
checklist, not a recommendation.
