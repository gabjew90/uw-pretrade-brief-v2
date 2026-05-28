"""Snapshot pipeline orchestrator. Background asyncio task; one cycle per 60s.

See spec §7 for the full sequence. Key responsibilities:
  1. Compute hot_15 from flow-alerts
  2. Compose tracked_universe = pinned ∪ indices ∪ sticky ∪ hot_15
  3. Refresh archive for the whole tracked universe (storage.py per-ticker TTL)
  4. Build dashboard rows from the hot_15 subset (cached, no fresh UW calls)
  5. Generate Gemini insights (5-min cache)
  6. Assemble Snapshot pydantic model, persist to JSONL + in-memory cache
"""
from __future__ import annotations
import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from typing import Any

from server import gates, insights, storage, universe
from server.schema import (DarkPool, Flow, Insights, OI,
                            OIStrike, Regime, Row, Snapshot)

log = logging.getLogger(__name__)

_POOL = ThreadPoolExecutor(max_workers=8)


async def refresh_snapshot() -> Snapshot:
    """One full cycle. Returns the freshly-built Snapshot."""
    loop = asyncio.get_running_loop()
    now = datetime.now(tz=timezone.utc)

    # 1. hot_15 from flow-alerts
    flow_alerts = await loop.run_in_executor(_POOL, partial(storage.fetch_flow_alerts, 100))
    if isinstance(flow_alerts, storage.UWFailure):
        log.error("flow-alerts fetch failed: %s", flow_alerts.message)
        return _empty_snapshot(now)
    hot_15 = universe.top_15_unique_tickers(flow_alerts)

    # 2. tracked_universe
    sticky = universe.StickyState(storage.load_sticky())
    sticky.touch(hot_15, now=now)
    sticky.decay(now=now)
    storage.save_sticky(sticky.to_dict())
    tracked = universe.compose_universe(hot_15=hot_15, sticky=sticky, now=now)

    # 3. Refresh archive for full tracked universe (cache will handle most)
    await asyncio.gather(*[
        _refresh_for_archive(t, is_hot=(t in hot_15), loop=loop)
        for t in tracked
    ], return_exceptions=True)

    # 4. Build dashboard rows from hot_15
    rows = []
    for ticker in hot_15:
        row = await _build_dashboard_row(ticker, loop=loop)
        rows.append(row)

    # 5. Insights
    for row in rows:
        row.insights = Insights(**insights.generate_insights(row.model_dump()))

    # 6. Assemble + persist
    snap = Snapshot(
        fetched_at=now,
        regime=_current_regime(),
        rows=rows,
    )
    storage.append_snapshot(snap.model_dump(mode="json"))
    log.info("snapshot built: hot=%d tracked=%d", len(hot_15), len(tracked))
    return snap


async def _refresh_for_archive(ticker: str, *, is_hot: bool, loop):
    """Fetch all per-ticker endpoints; storage.fetch_* writes parquet on cache miss."""
    tasks = [
        loop.run_in_executor(_POOL, partial(storage.fetch_spot_exposures_strike, ticker, is_hot)),
        loop.run_in_executor(_POOL, partial(storage.fetch_oi_strike, ticker, is_hot)),
        loop.run_in_executor(_POOL, partial(storage.fetch_volatility, ticker, is_hot)),
        loop.run_in_executor(_POOL, partial(storage.fetch_interpolated_iv, ticker, is_hot)),
        loop.run_in_executor(_POOL, partial(storage.fetch_darkpool, ticker, is_hot)),
        loop.run_in_executor(_POOL, partial(storage.fetch_earnings, ticker, is_hot)),
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _build_dashboard_row(ticker: str, *, loop) -> Row:
    is_hot = True
    spot_data = await loop.run_in_executor(_POOL, partial(storage.fetch_spot_exposures_strike, ticker, is_hot))
    oi_data = await loop.run_in_executor(_POOL, partial(storage.fetch_oi_strike, ticker, is_hot))
    vol_data = await loop.run_in_executor(_POOL, partial(storage.fetch_volatility, ticker, is_hot))
    ivr_data = await loop.run_in_executor(_POOL, partial(storage.fetch_interpolated_iv, ticker, is_hot))
    dp_data = await loop.run_in_executor(_POOL, partial(storage.fetch_darkpool, ticker, is_hot))
    earn_data = await loop.run_in_executor(_POOL, partial(storage.fetch_earnings, ticker, is_hot))

    failures = [r.endpoint for r in (spot_data, oi_data, vol_data, ivr_data, dp_data, earn_data)
                if isinstance(r, storage.UWFailure)]

    spot = _extract_spot(spot_data)
    oi = _extract_oi(oi_data)
    iv_term = _extract_iv_curve(vol_data)
    ivr = _extract_ivr(ivr_data)
    dp = _extract_darkpool(dp_data)
    days_to_earn = _extract_days_to_earnings(earn_data)
    flip_pct, wall_up_pct, wall_dn_pct, gex_sign, agg_b = _extract_gex(spot_data, spot)

    raw_row = {
        "ticker": ticker,
        "spot": spot,
        "direction": "calls",
        "flip_dist_pct": flip_pct,
        "wall_up_dist_pct": wall_up_pct,
        "wall_dn_dist_pct": wall_dn_pct,
        "ivr": ivr,
        "days_to_earnings": days_to_earn,
        "flow_rank_cross": 50,
        "oi": {"strikes": oi},
    }
    g = gates.compute_gates(raw_row, history=None)
    gm = gates.compute_gate_method(raw_row, history=None)

    return Row(
        ticker=ticker,
        spot=spot,
        direction="calls",
        gates=g,
        gate_method=gm,
        flow=Flow(alerts=0, premium_usd=0.0, rank_cross=50),
        oi=OI(strikes=[OIStrike(**s) for s in oi]),
        flip_dist_pct=flip_pct,
        wall_up_dist_pct=wall_up_pct,
        wall_dn_dist_pct=wall_dn_pct,
        agg_gamma_b=agg_b,
        gex_sign=gex_sign,
        ivr=ivr,
        days_to_earnings=days_to_earn,
        iv_term_curve=iv_term,
        dark_pool=dp,
        _failures=failures,
    )


# ── Extractors ────────────────────────────────────────────────────────────────

def _extract_spot(data: Any) -> float:
    if isinstance(data, storage.UWFailure) or not isinstance(data, dict):
        return 0.0
    rows = data.get("data", [])
    if not rows:
        return 0.0
    return float(rows[0].get("spot_price", rows[0].get("strike", 0.0)) or 0.0)


def _extract_oi(data: Any) -> list[dict]:
    if isinstance(data, storage.UWFailure) or not isinstance(data, dict):
        return []
    rows = data.get("data", [])[:5]
    out = []
    for r in rows:
        prev = int(r.get("prev_open_interest", 0))
        today = int(r.get("open_interest", 0))
        pct = round(((today - prev) / prev * 100) if prev else 0, 1)
        out.append({"strike": float(r.get("strike", 0)), "prev": prev,
                    "today": today, "pct": pct})
    return out


def _extract_iv_curve(data: Any) -> list[float]:
    if isinstance(data, storage.UWFailure) or not isinstance(data, dict):
        return []
    rows = data.get("data", [])
    return [float(r.get("iv", 0)) for r in rows[:4]]


def _extract_ivr(data: Any) -> int:
    if isinstance(data, storage.UWFailure) or not isinstance(data, dict):
        return 50
    rows = data.get("data", [])
    if not rows:
        return 50
    return int(rows[0].get("iv_rank", 50))


def _extract_darkpool(data: Any) -> DarkPool:
    if isinstance(data, storage.UWFailure) or not isinstance(data, dict):
        return DarkPool()
    rows = data.get("data", [])
    net = sum(float(r.get("price", 0)) * int(r.get("size", 0)) for r in rows)
    return DarkPool(net_premium_usd=net, pct_of_volume=0, trend="neutral")


def _extract_days_to_earnings(data: Any) -> int | None:
    if isinstance(data, storage.UWFailure) or not isinstance(data, dict):
        return None
    rows = data.get("data", [])
    if not rows:
        return None
    iso = rows[0].get("report_date") or rows[0].get("date")
    if not iso:
        return None
    try:
        report = datetime.fromisoformat(iso).date()
        today = datetime.now(tz=timezone.utc).date()
        return (report - today).days
    except Exception:
        return None


def _extract_gex(spot_data: Any, spot: float) -> tuple[float, float, float, str, float]:
    if isinstance(spot_data, storage.UWFailure) or not isinstance(spot_data, dict):
        return 0.0, 5.0, 5.0, "POS", 0.0
    rows = spot_data.get("data", [])
    if not rows or spot <= 0:
        return 0.0, 5.0, 5.0, "POS", 0.0
    strikes_with_gamma = [(float(r.get("strike", 0)),
                            float(r.get("call_gamma_oi", 0)) - float(r.get("put_gamma_oi", 0)))
                          for r in rows]
    above = [(s, g) for s, g in strikes_with_gamma if s > spot]
    below = [(s, g) for s, g in strikes_with_gamma if s < spot]
    wall_up = max(above, key=lambda x: abs(x[1]), default=(spot * 1.05, 0))[0]
    wall_dn = max(below, key=lambda x: abs(x[1]), default=(spot * 0.95, 0))[0]
    flip = min(strikes_with_gamma, key=lambda x: abs(x[1]), default=(spot, 0))[0]
    agg = sum(g for _, g in strikes_with_gamma) / 1e9
    return ((flip - spot) / spot * 100,
            (wall_up - spot) / spot * 100,
            (spot - wall_dn) / spot * 100,
            "POS" if agg >= 0 else "NEG",
            agg)


def _current_regime() -> Regime:
    label = os.environ.get("REGIME", "normal").lower()
    if label not in ("normal", "risk-off"):
        label = "normal"
    detail = os.environ.get("REGIME_DETAIL_TEXT", "")
    vix = 0.0
    if "VIX" in detail.upper():
        try:
            vix = float(detail.split("VIX")[1].split()[0])
        except (ValueError, IndexError):
            pass
    return Regime(label=label, detail=detail, vix=vix)


def _empty_snapshot(now: datetime) -> Snapshot:
    return Snapshot(fetched_at=now, regime=_current_regime(), rows=[])
