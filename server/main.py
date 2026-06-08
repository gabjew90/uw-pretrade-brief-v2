"""FastAPI entry — one process. Serves the dumb frontend + the typed view model.

The HTTP layer is thin: it runs the pipeline (ingest→normalize→derive→decide→present)
and returns a `ViewModel`. No business logic lives here. The browser fetches the view
model and renders it; it computes nothing (the one rule).

This is the skeleton: `/health` is live; `/api/view/{ticker}` runs the (stubbed)
pipeline end-to-end so the wiring is exercisable before signals exist.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server.config import settings
from server.models import ViewModel
from server.pipeline.decide import decide
from server.pipeline.derive import derive_all
from server.pipeline.present import present
from server.services import clock

app = FastAPI(title="UW Pretrade Brief v3")

_STATIC = Path(__file__).resolve().parent.parent / "static"


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "replay": settings.replay,
        "phase": clock.phase().value,
        "session_date": clock.session_date().isoformat(),
        "oi_settled_through": clock.oi_settled_through().isoformat(),
    }


@app.get("/api/view/{ticker}", response_model=ViewModel)
def view(ticker: str) -> ViewModel:
    """Run the pipeline for one ticker and return the view model. With no signals
    registered yet this returns an empty-but-well-formed view model (proves the wiring
    end-to-end)."""
    ticker = ticker.upper()
    asof = clock.session_date().isoformat()
    # ingest + normalize happen per signal's needs once wired; for the skeleton we run
    # derive over an empty canonical map so the contract is exercised.
    canon: dict = {}
    signals = derive_all(canon, asof=asof)
    verdict = decide(signals)
    return present(ticker, signals, verdict, as_of=asof)


@app.get("/")
def root() -> FileResponse:
    index = _STATIC / "index.html"
    if index.exists():
        return FileResponse(index, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return JSONResponse({"detail": "static/index.html not found"}, status_code=404)


# Static assets (after routes so "/" is handled above).
if _STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
