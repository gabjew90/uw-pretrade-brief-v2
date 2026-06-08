# UW Pretrade Brief v3

A clean, properly-bounded rebuild of v2. Single FastAPI process, local parquet +
DuckDB, a typed 5-stage pipeline, and **one rule: the frontend computes nothing.**

See [`CLAUDE.md`](CLAUDE.md) for the locked architecture and
[`docs/architecture.md`](docs/architecture.md) for the full rationale.

## Pipeline

```
ingest → normalize → derive → decide → present
(raw)    (canonical)  (signals) (verdict) (view model → dumb frontend)
```

Cross-cutting services: **clock** (sessions/settlement), **provenance** (source +
quality + as_of on every value), **governor** (UW budget/priority), **storage**
(append-only bronze/silver/gold parquet, DuckDB read layer).

## Setup

```bash
uv venv && uv pip install -e ".[dev]"      # or: python -m venv .venv && pip install -e ".[dev]"
cp .env.example .env                        # fill from your EXISTING v2 keys (reused, same names)
.venv/Scripts/python -m uvicorn server.main:app --port 8000   # Windows; use .venv/bin on *nix
```

Then open http://localhost:8000 — the shell renders `/api/view/SPY` (empty until
signals are registered).

## Offline / replay

`REPLAY=1` serves the captured **bronze** archive and never calls UW (the governor
denies live calls; ingest reads the latest bronze part). No market, no key, no limits.

## Status

**Scaffold.** Cross-cutting services are real (clock has tests); the 5 pipeline stages
are typed contract stubs awaiting the signal/tile instructions. Keys are **reused from
v2** (same env var names — see `.env.example`).

## Tests

```bash
.venv/Scripts/python -m pytest -q
```
