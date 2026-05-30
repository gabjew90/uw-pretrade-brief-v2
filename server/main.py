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
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from server import market_hours
from server import snapshot as snapshot_mod
from server.schema import Snapshot

log = logging.getLogger(__name__)

_snapshot_cache: dict[str, Snapshot | None] = {"latest": None}
_REFRESH_INTERVAL_SECONDS = 120  # 15 tickers × 9 endpoints = 135 calls/cycle; 120s = ~68/min, fits 120/min budget
# When the market is closed we re-check the clock every 5 min instead of every
# 120s — resumes within 5 min of the open without spinning UW-free clock checks.
_CLOSED_RECHECK_SECONDS = 300


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_refresh_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _next_cached_snapshot(current: Snapshot | None, fresh: Snapshot,
                          now: datetime) -> Snapshot:
    """Decide what stays in the cache after one refresh cycle.

    A refresh that yields rows is real data and replaces the cache. A refresh
    that yields NO rows means the upstream fetch failed — `refresh_snapshot`
    returns `_empty_snapshot` when the leading flow-alerts call 429s/errors. In
    that case we KEEP the last good snapshot and stamp `stale_since` rather than
    blanking the dashboard. Only when there is no prior good snapshot (cold boot
    mid-outage) do we surface the empty/warming snapshot so /health reports it
    and the loop keeps retrying instead of freezing a blank.

    Regression guard for the 2026-05-29 outage: a single in-window 429 used to
    overwrite good data with `rows: []`, which the market-gate then froze for
    the whole closed period.
    """
    if fresh.rows:
        return fresh
    if current is not None and current.rows:
        if current.stale_since is None:
            current.stale_since = now
        return current
    return fresh


async def _refresh_loop():
    while True:
        # Kill-switch: SNAPSHOT_PAUSED=true skips the UW call entirely. Use this
        # to give UW's rate-limit window time to fully reset without our traffic.
        if os.environ.get("SNAPSHOT_PAUSED", "").lower() in ("1", "true", "yes"):
            log.info("snapshot loop paused (SNAPSHOT_PAUSED env var set); sleeping %ds",
                     _REFRESH_INTERVAL_SECONDS)
            await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)
            continue

        # Market-hours gate: options data only moves during RTH. Outside the
        # window we hold the last-good snapshot and re-check every 5 min. Set
        # MARKET_GATE_DISABLED=true to force 24/7 (e.g. for debugging).
        # We gate on having a GOOD (non-empty) snapshot, not merely a non-None
        # one: a cold boot mid-outage produces an empty snapshot, and we must
        # keep retrying it rather than freezing a blank dashboard until the open.
        gate_off = os.environ.get("MARKET_GATE_DISABLED", "").lower() in ("1", "true", "yes")
        cached = _snapshot_cache.get("latest")
        have_good = cached is not None and bool(cached.rows)
        if not gate_off and have_good and not market_hours.market_is_open():
            log.info("market closed; holding last-good snapshot, re-check in %ds",
                     _CLOSED_RECHECK_SECONDS)
            await asyncio.sleep(_CLOSED_RECHECK_SECONDS)
            continue

        try:
            fresh = await snapshot_mod.refresh_snapshot()
            kept = _next_cached_snapshot(cached, fresh, datetime.now(tz=timezone.utc))
            if kept is cached and not fresh.rows:
                log.warning("refresh produced 0 rows (upstream failure); holding "
                            "last-good snapshot, stale_since=%s", kept.stale_since)
            _snapshot_cache["latest"] = kept
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
