# CLAUDE.md

Project: **UW Pretrade Brief v2** — FastAPI port of the v1 prototype with persistent archive.

## Tech stack (locked)

- Python 3.11
- FastAPI + uvicorn
- requests (UW client kept sync; wrapped in ThreadPoolExecutor for parallelism)
- pyarrow (parquet archive)
- google-genai (Gemini Flash-Lite)
- pydantic v2
- pytest + responses + pytest-mock + pytest-asyncio

Do **not** suggest Streamlit (this is the v2 specifically pivoting away). Do not suggest WebSockets, Redis, or external DBs without explicit override.

## Behavior

- Personal use only. The deployed Railway URL is the operator's; others fork + self-host with their own keys.
- Never claim alpha, edge, or backtested win rates.
- Frequent commits, conventional-commit style.
- Phone-only operator: paste file contents or diffs into chat after edits; upload binaries to litterbox.

## Local replay / offline dev (NO market, NO UW key, NO rate limits)

Develop and verify the WHOLE dashboard offline against the real captured prod
archive — don't wait for market hours or burn UW budget:

```
python scripts/pull_archive.py --token "$BACKFILL_TOKEN"   # downloads prod DATA_DIR → ./data (gitignored)
DATA_DIR=./data REPLAY=1 .venv/Scripts/python -m uvicorn server.main:app --port 8000
```

Then open http://localhost:8000 and click any ticker — all tiles, including the
on-demand Tile 3 gamma map and Tile 4 contract picker, render from the archive.
REPLAY=1 skips the background loop and forces the request path to `cached_only`
(reads captured parquet, never calls UW); the boot-seed serves the archived
snapshot. In cached-only mode the parquet TTL is ignored (replay reads aged
data). The `/admin/export` endpoint (token-guarded by BACKFILL_TOKEN) streams a
tar.gz of DATA_DIR for re-pulling a fresh archive.

## Behavior — guardrails

- The `frontend-design` plugin is **allowed** for frontend work. The earlier rule
  restricting `static/index.html` to the 6 marker-bracketed V2-EDIT zones is
  **relaxed**: deliberate edits to the prototype HTML/CSS/JS (including the tile
  render functions) are permitted when they serve a real improvement. When a
  sanctioned change trips `tests/test_html_preservation.py`, update that test to
  match the new intent rather than contorting the code to keep the old diff —
  but keep the test meaningful (it still guards against *accidental* drift).
- Don't suggest dropping the storage layer "for simplicity" — it's load-bearing for the v0.2 percentile gates.
- Don't suggest hosting on Streamlit Cloud.
