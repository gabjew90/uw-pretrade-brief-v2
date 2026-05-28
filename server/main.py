"""FastAPI app entrypoint. Lifespan task refreshes snapshot every 60s.

Routes:
  GET /              → prototype HTML hydrated with __SNAPSHOT__ from cache
  GET /snapshot.json → cached snapshot as JSON (frontend polls every 60s)
  GET /health        → liveness + snapshot age
"""
from __future__ import annotations
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from server import snapshot as snapshot_mod
from server.schema import Snapshot

log = logging.getLogger(__name__)

_snapshot_cache: dict[str, Snapshot | None] = {"latest": None}
_REFRESH_INTERVAL_SECONDS = 120  # 15 tickers × 9 endpoints = 135 calls/cycle; 120s = ~68/min, fits 120/min budget


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_refresh_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _refresh_loop():
    while True:
        # Kill-switch: SNAPSHOT_PAUSED=true skips the UW call entirely. Use this
        # to give UW's rate-limit window time to fully reset without our traffic.
        if os.environ.get("SNAPSHOT_PAUSED", "").lower() in ("1", "true", "yes"):
            log.info("snapshot loop paused (SNAPSHOT_PAUSED env var set); sleeping %ds",
                     _REFRESH_INTERVAL_SECONDS)
        else:
            try:
                snap = await snapshot_mod.refresh_snapshot()
                _snapshot_cache["latest"] = snap
            except Exception as e:
                log.exception("snapshot refresh failed: %s", e)
        await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)


app = FastAPI(lifespan=lifespan, title="UW Pretrade Brief v2")

_STATIC_DIR = Path(__file__).parent.parent / "static"
_HYDRATION_MARKER = "<!-- HYDRATION_TARGET -->"


@app.get("/", response_class=HTMLResponse)
async def root():
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    snap = _snapshot_cache.get("latest")
    if snap is None:
        snapshot_json = "null"
    else:
        snapshot_json = json.dumps(snap.model_dump(mode="json"), default=str)
    hydration = f"<script>window.__SNAPSHOT__ = {snapshot_json};</script>"
    return HTMLResponse(html.replace(_HYDRATION_MARKER, hydration))


@app.get("/snapshot.json")
async def snapshot_json():
    snap = _snapshot_cache.get("latest")
    if snap is None:
        return JSONResponse({"status": "warming", "rows": []}, status_code=200)
    return JSONResponse(snap.model_dump(mode="json"))


@app.get("/health")
async def health():
    snap = _snapshot_cache.get("latest")
    now = datetime.now(tz=timezone.utc)
    if snap is None:
        return {"status": "ok", "snapshot_age_s": None, "snapshot_fetched_at": None,
                "tickers": 0}
    age_s = int((now - snap.fetched_at.replace(tzinfo=timezone.utc)).total_seconds())
    return {"status": "ok", "snapshot_age_s": age_s,
            "snapshot_fetched_at": snap.fetched_at.isoformat(),
            "tickers": len(snap.rows)}
