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

## Behavior — guardrails

- Don't touch the prototype HTML structure beyond the 6 marker-bracketed V2-EDIT zones. The diff test enforces this.
- Don't suggest dropping the storage layer "for simplicity" — it's load-bearing for the v0.2 percentile gates.
- Don't suggest hosting on Streamlit Cloud.
