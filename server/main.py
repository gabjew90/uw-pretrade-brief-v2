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

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from server import backfill, budget, market_hours, storage, tile3_detail, tile4
from server import snapshot as snapshot_mod
from server.schema import Snapshot

log = logging.getLogger(__name__)

_snapshot_cache: dict[str, Snapshot | None] = {"latest": None}
_REFRESH_INTERVAL_SECONDS = 120  # 15 tickers × 9 endpoints = 135 calls/cycle; 120s = ~68/min, fits 120/min budget
# When the market is closed we re-check the clock every 5 min instead of every
# 120s — resumes within 5 min of the open without spinning UW-free clock checks.
_CLOSED_RECHECK_SECONDS = 300


def _seed_cache_from_disk() -> None:
    """Seed the in-memory snapshot cache from the last persisted snapshot on
    boot, so a cold-boot/redeploy (off-hours, or while UW is rate-limited) serves
    last-good data instead of a blank dashboard. Marked stale_since since it's by
    definition not a fresh live read. No-op if nothing is persisted yet."""
    if _snapshot_cache.get("latest") is not None:
        return
    raw = storage.read_last_snapshot()
    if not raw:
        return
    try:
        snap = Snapshot.model_validate(raw)
        if snap.stale_since is None:
            snap.stale_since = datetime.now(tz=timezone.utc)
        _snapshot_cache["latest"] = snap
        log.info("seeded snapshot cache from disk: rows=%d fetched_at=%s",
                 len(snap.rows), snap.fetched_at)
    except Exception as e:
        log.warning("could not seed cache from disk: %s", e)


def _atomic_view(build_fn, view: str, ticker: str):
    """Build an on-demand view (tile3/tile4/lookup) with ATOMIC freshness — no
    mixing of live + last-good within one view.

    - Live build SUCCEEDS (ok/stand_down) → persist the COMPLETE payload as one
      unit (storage.save_view) and return it fresh.
    - Live build FAILS (unavailable — 429/off-hours/error) → serve the WHOLE last
      persisted view from ONE coherent moment (storage.read_view), stale-marked.
      This replaces the old per-endpoint cached_only reassembly, which stitched
      each endpoint's most-recent archive row from DIFFERENT moments.
    - Nothing persisted yet → the honest 'unavailable' stands."""
    out = build_fn()
    if isinstance(out, dict) and out.get("status") in ("ok", "stand_down"):
        storage.save_view(view, ticker, out)
        return out
    last_good = storage.read_view(view, ticker)
    if isinstance(last_good, dict) and last_good.get("status") in ("ok", "stand_down"):
        return {**last_good, "stale": True}
    return out


def _replay_enabled() -> bool:
    """Local replay/dev mode: serve the whole dashboard from a captured DATA_DIR
    archive with NO live UW calls and NO background loop. Run offline anytime:
        DATA_DIR=./data REPLAY=1 uvicorn server.main:app
    The boot-seed serves the archived snapshot; the on-demand tile routes read
    the captured UW responses (cached_only) instead of calling UW."""
    return os.environ.get("REPLAY", "").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Restore today's UW call count from the volume so a cold-boot/redeploy
    # doesn't reset the budget meter to 0 (which masked the real cap 2026-06-01).
    budget.load_persisted()
    # Seed the snapshot cache from the last persisted good snapshot, so a
    # cold-boot serves last-close data instead of blank (off-hours / mid-outage).
    _seed_cache_from_disk()
    # REPLAY mode: no background loop (no live UW); the dashboard runs entirely
    # off the captured archive.
    task = None if _replay_enabled() else asyncio.create_task(_refresh_loop())
    yield
    if task is None:
        return
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


@app.get("/api/tile3/{ticker}")
async def tile3_detail_route(ticker: str):
    """Rich Tile 3 (Phase 2): per-expiry gamma map + OI/Vol + drift. ON-DEMAND —
    fetches live UW when a ticker is selected (cached 5 min). A human views 2-3
    tickers/session, so this is far cheaper than prewarming all 15 each cycle.
    spot/direction from the cached snapshot."""
    t = ticker.upper()
    snap = _snapshot_cache.get("latest")
    row = next((r for r in (snap.rows if snap and snap.rows else []) if r.ticker == t), None)
    flow_spot = float(getattr(row, "spot", 0.0) or 0.0)
    direction = getattr(row, "direction", "calls") or "calls"

    def _run():
        if _replay_enabled():
            with storage.cached_only():
                return tile3_detail.build_tile3_detail(t, flow_spot, direction)
        # Live-first; on failure replay the whole last-good view (one moment).
        return _atomic_view(
            lambda: tile3_detail.build_tile3_detail(t, flow_spot, direction), "tile3", t)
    detail = await asyncio.to_thread(_run)
    return JSONResponse(detail)


@app.get("/api/tile4/{ticker}")
async def tile4_route(ticker: str):
    """Contract Picker & Final Gate. ON-DEMAND — fetches the vol/greeks/event data
    live when a ticker is selected (cached 5 min). Reuses the cached snapshot
    row's Tiles 1-3 outputs (direction, flow strikes, OI campaign, walls). See
    server/tile4.py."""
    t = ticker.upper()
    snap = _snapshot_cache.get("latest")
    row = next((r for r in (snap.rows if snap and snap.rows else []) if r.ticker == t), None)
    if row is None:
        return JSONResponse({"status": "unavailable", "ticker": t, "reason": "not in snapshot"})
    spot = float(getattr(row, "spot", 0) or 0)
    # Flow value per strike: sum smart-money premium at each strike (real $, not
    # just a yes/no membership) so the picker can show it.
    flow_det = getattr(row, "flow_alerts_detail", None) or []
    flow_premium_by_strike: dict | None = None
    if flow_det:
        flow_premium_by_strike = {}
        for fa in flow_det:
            k = getattr(fa, "strike", None)
            if k:
                flow_premium_by_strike[k] = (flow_premium_by_strike.get(k, 0.0)
                                             + float(getattr(fa, "total_premium", 0) or 0))
    # Campaign value per strike: OI Δ% + trend from Tile 2's per-strike history.
    t2 = getattr(row, "tile2", None)
    oi_by_strike: dict | None = None
    if t2 is not None and getattr(t2, "strikes", None):
        oi_by_strike = {}
        for s in t2.strikes:
            prev = (getattr(s, "delta_oi", 0) or 0)
            sessions = getattr(s, "sessions", None) or []
            base = 0
            if len(sessions) >= 2 and getattr(sessions[-2], "oi", 0):
                base = sessions[-2].oi
            delta_pct = (prev / base * 100) if base else None
            oi_by_strike[s.strike] = {"delta_pct": delta_pct, "trend": getattr(s, "trend", "flat")}
    wu, wd = getattr(row, "wall_up_dist_pct", None), getattr(row, "wall_dn_dist_pct", None)
    ctx = {
        "spot": spot,
        "direction": getattr(row, "direction", "calls"),
        "flow_premium_by_strike": flow_premium_by_strike,
        "oi_by_strike": oi_by_strike,
        "call_wall": spot * (1 + wu / 100) if (spot and wu is not None) else None,
        "put_wall": spot * (1 - wd / 100) if (spot and wd is not None) else None,
    }
    def _run():
        if _replay_enabled():
            with storage.cached_only():
                return tile4.build_tile4(t, ctx)
        return _atomic_view(lambda: tile4.build_tile4(t, ctx), "tile4", t)
    detail = await asyncio.to_thread(_run)
    return JSONResponse(detail)


@app.get("/api/lookup/{ticker}")
async def lookup_route(ticker: str):
    """Search-any-ticker: build a FULL dashboard row for an arbitrary ticker on
    demand (~15 UW calls) and return it in the same shape as a snapshot row. The
    frontend injects it into ROWS and selects it, so it behaves like a hot
    ticker. In REPLAY mode the build reads the captured archive (cached_only)."""
    t = ticker.upper()
    try:
        if _replay_enabled():
            with storage.cached_only():
                row = await snapshot_mod.build_single_row(t)
        else:
            row = await snapshot_mod.build_single_row(t)
    except Exception as e:
        log.warning("lookup %s failed: %s", t, e)
        return JSONResponse({"status": "unavailable", "ticker": t, "reason": str(e)})
    if row is None:
        return JSONResponse({"status": "unavailable", "ticker": t,
                             "reason": "no data for this ticker"})
    return JSONResponse(row.model_dump(mode="json"))


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
    _b = budget.snapshot()
    uw_block = {k: _b[k] for k in
                ("calls_1m", "calls_today", "daily_cap", "budget_pct",
                 "minute_remaining", "source")}
    if snap is None:
        return {"status": "ok", "snapshot_age_s": None, "snapshot_fetched_at": None,
                "tickers": 0, "uw": uw_block}
    age_s = int((now - snap.fetched_at.replace(tzinfo=timezone.utc)).total_seconds())
    return {"status": "ok", "snapshot_age_s": age_s,
            "snapshot_fetched_at": snap.fetched_at.isoformat(),
            "tickers": len(snap.rows), "uw": uw_block}


@app.post("/admin/backfill")
async def admin_backfill(days: int = 30, token: str | None = None):
    """One-shot historical OI backfill. Dormant unless BACKFILL_TOKEN is set;
    runs the probe synchronously (verdict in the response) and, if supported,
    launches the full gap-fill in the background. See server/backfill.py."""
    expected = os.environ.get("BACKFILL_TOKEN")
    if not expected:
        raise HTTPException(status_code=403, detail="backfill disabled (BACKFILL_TOKEN unset)")
    if token != expected:
        raise HTTPException(status_code=403, detail="invalid token")

    snap = _snapshot_cache.get("latest")
    tickers = [r.ticker for r in snap.rows] if snap and snap.rows else []
    if not tickers:
        raise HTTPException(status_code=409, detail="no live snapshot yet — retry once data is warm")

    days = max(1, min(days, 30))
    result = backfill.probe(tickers, days)
    if result.get("probe") != "ok":
        return JSONResponse(result, status_code=200)

    asyncio.create_task(asyncio.to_thread(backfill.backfill_oi_history, tickers, days))
    return JSONResponse({**result, "backfill": "started", "tickers": len(tickers)},
                        status_code=202)


@app.get("/admin/export")
async def admin_export(token: str | None = None, days: int = 7):
    """Stream a .tar.gz of the DATA_DIR (snapshots.jsonl + sticky + recent parquet
    archive) so the REAL prod data can be pulled down for local replay/dev.
    Token-guarded (reuses BACKFILL_TOKEN). `days` bounds the parquet archive to
    the most recent N dt= partitions to keep the download manageable; the
    snapshot/sticky files are always included."""
    import io
    import tarfile
    from datetime import timedelta
    from fastapi.responses import Response

    expected = os.environ.get("BACKFILL_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="forbidden")

    data_dir = storage._data_dir()
    cutoff = (datetime.now(tz=timezone.utc).date() - timedelta(days=max(0, days))).isoformat()

    def _include(p: Path) -> bool:
        # always include top-level files (snapshots.jsonl, sticky.json, etc.)
        rel = p.relative_to(data_dir)
        if rel.parts and rel.parts[0] == "raw":
            # raw/endpoint=.../dt=YYYY-MM-DD/... — keep only recent dt= partitions
            for part in rel.parts:
                if part.startswith("dt="):
                    return part[3:] >= cutoff
        return True

    def _build_tar() -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            if data_dir.is_dir():
                for p in sorted(data_dir.rglob("*")):
                    if p.is_file() and _include(p):
                        tar.add(p, arcname=str(p.relative_to(data_dir)))
        return buf.getvalue()

    blob = await asyncio.to_thread(_build_tar)
    return Response(content=blob, media_type="application/gzip",
                    headers={"Content-Disposition": "attachment; filename=uw_data.tar.gz"})


