"""FastAPI entry — one process. Serves the dumb frontend + the typed view model.

The HTTP layer is thin: it runs the pipeline (ingest→normalize→derive→decide→present)
and returns a `ViewModel`. No business logic lives here. The browser fetches the view
model and renders it; it computes nothing (the one rule).

This is the skeleton: `/health` is live; `/api/view/{ticker}` runs the (stubbed)
pipeline end-to-end so the wiring is exercisable before signals exist.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from fastapi import HTTPException

from server.config import settings
from server.models import ViewModel
from server.pipeline.orchestrate import build_grid, build_view
from server.services import clock, maintenance
from server.services.governor import governor

_STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Restore today's UW call count so a redeploy doesn't reset the daily meter to 0.
    governor.load_persisted()
    # Arm the nightly 03:00-ET maintenance thread (backup/compact/backtest — all offline;
    # in-app because the Railway volume mounts to this one service).
    maintenance.start()
    yield


app = FastAPI(title="UW Pretrade Brief v3", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "replay": settings.replay,
        "phase": clock.phase().value,
        "session_date": clock.session_date().isoformat(),
        "oi_settled_through": clock.oi_settled_through().isoformat(),
        "budget": governor.snapshot(),
        "maintenance": maintenance.last_run(),
    }


@app.post("/admin/maintenance")
def run_maintenance(token: str) -> dict:
    """Manually trigger the nightly maintenance (backup/compact/backtest) — token-guarded
    (BACKFILL_TOKEN). Used to verify the tasks on prod without waiting for 03:00 ET."""
    if not settings.backfill_token or token != settings.backfill_token:
        raise HTTPException(status_code=403, detail="bad token")
    return maintenance.run_all()


# Session view cache: /api/view fills it; the grid reads it. Verdicts on the landing
# require full per-ticker builds, which the request-driven platform deliberately doesn't
# sweep in the background ([[basic-platform]]) — so rows carry real n/N for names
# evaluated this session, and unswept hot names sink to the bottom as 0/N unknown.
_VIEWS: dict[str, ViewModel] = {}


@app.get("/api/grid")
def grid() -> dict:
    """GridVM (Present Contract Extensions §5): best direction per ticker, sorted n
    desc, PERFECT pinned top — server-sorted. Hot names from ONE cross-ticker flow
    call; n/N from the session view cache (on-demand tier: open a name to run its
    gates)."""
    hot = [r["ticker"] for r in (build_grid().get("rows") or [])]
    rows = []
    for t in dict.fromkeys(list(_VIEWS) + hot):
        vm = _VIEWS.get(t)
        if vm is not None and vm.best and (vm.calls or vm.puts):
            d = vm.puts if vm.best == "puts" else vm.calls
            rows.append({"ticker": t, "direction": d["direction"], "state": d["state"],
                         "green": d["green"], "total": d["total"], "tag": d.get("tag")})
        else:
            rows.append({"ticker": t, "direction": "—", "state": "NOT NOW",
                         "green": 0, "total": 4, "tag": None})
    rows.sort(key=lambda r: (r["state"] != "PERFECT", -r["green"], r["ticker"]))
    as_of = clock._et(None).strftime("%H:%M ET")
    evaluated = sum(1 for r in rows if r["direction"] != "—")
    return {"asOf": as_of,
            "status": f"{len(rows)} hot names by opening premium · {evaluated} evaluated "
                      "this session · open a name to run its gates (on-demand tier)",
            "rows": rows}


@app.get("/api/history/{ticker}")
def history(ticker: str) -> dict:
    """The track record: what the tool said each archived session and what the underlying
    did next (regenerated nightly by the backtest task). The 'was it right' data."""
    import json as _json
    path = settings.gold / "backtest" / "daily" / "signal_history.jsonl"
    if not path.exists():
        return {"rows": [], "note": "accumulates nightly at 03:30 ET"}
    rows = [r for r in (_json.loads(line) for line in
                        path.read_text(encoding="utf-8").splitlines() if line.strip())
            if r.get("ticker") == ticker.upper()]
    scored = [r for r in rows if r.get("called_right") is not None]
    right = sum(1 for r in scored if r["called_right"])
    return {"rows": rows,
            "note": f"{right}/{len(scored)} direction calls matched the next session's move"
                    if scored else "no scored sessions yet"}


@app.get("/api/view/{ticker}", response_model=ViewModel)
def view(ticker: str) -> ViewModel:
    """Run the full pipeline for one ticker and return the view model. Ingest is
    governor-gated (live, or bronze in REPLAY); on failure the view degrades honestly
    (direction `unavailable`, never guessed). The browser renders this and computes
    nothing (the one rule). Built views feed the landing grid's n/N (session cache)."""
    vm = build_view(ticker)
    _VIEWS[ticker.upper()] = vm
    return vm


@app.get("/")
def root() -> FileResponse:
    index = _STATIC / "index.html"
    if index.exists():
        return FileResponse(index, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return JSONResponse({"detail": "static/index.html not found"}, status_code=404)


# Static assets (after routes so "/" is handled above).
if _STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
