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

from server import gates, insights, storage, uw, universe
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
    flow_by_ticker = _aggregate_flow_per_ticker(flow_alerts, hot_15)

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
        flow_info = flow_by_ticker.get(ticker, {"alerts": 0, "premium_usd": 0.0,
                                                 "rank_cross": 50, "spot": 0.0})
        row = await _build_dashboard_row(ticker, flow_info=flow_info, loop=loop)
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


async def _build_dashboard_row(ticker: str, *, flow_info: dict, loop) -> Row:
    is_hot = True
    spot_data = await loop.run_in_executor(_POOL, partial(storage.fetch_spot_exposures_strike, ticker, is_hot))
    oi_data = await loop.run_in_executor(_POOL, partial(storage.fetch_oi_strike, ticker, is_hot))
    vol_data = await loop.run_in_executor(_POOL, partial(storage.fetch_volatility, ticker, is_hot))
    ivr_data = await loop.run_in_executor(_POOL, partial(storage.fetch_interpolated_iv, ticker, is_hot))
    dp_data = await loop.run_in_executor(_POOL, partial(storage.fetch_darkpool, ticker, is_hot))
    earn_data = await loop.run_in_executor(_POOL, partial(storage.fetch_earnings, ticker, is_hot))

    failures = [r.endpoint for r in (spot_data, oi_data, vol_data, ivr_data, dp_data, earn_data)
                if isinstance(r, storage.UWFailure)]

    # Spot: prefer flow_alerts.underlying_price (live), then any payload root
    spot = flow_info.get("spot") or 0.0
    if not spot:
        spot = uw.extract_spot(spot_data, oi_data, ivr_data) or 0.0
    oi = _extract_oi(oi_data)
    iv_term = _extract_iv_curve(vol_data)
    ivr = _extract_ivr(ivr_data, vol_data)
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
        "flow_rank_cross": flow_info.get("rank_cross", 50),
        "oi": {"strikes": oi},
    }
    g = gates.compute_gates(raw_row, history=None)
    gm = gates.compute_gate_method(raw_row, history=None)

    return Row(
        ticker=ticker,
        spot=spot,
        direction="calls",
        # The prototype's render functions gate on is_synthetic to decide
        # between nested-data shape (true) and flat-top-level shape (false).
        # Our v2 data shape matches the synthetic-style nesting (row.flow.*,
        # row.oi.strikes, row.dark_pool.*, row.news_items, row.sector_*),
        # so flag every Row as synthetic-shape to make the renderers consume
        # our nested fields correctly. Semantically the data is live — this
        # flag controls render-shape selection, not data provenance.
        is_synthetic=True,
        gates=g,
        gate_method=gm,
        flow=Flow(
            alerts=int(flow_info.get("alerts", 0)),
            premium_usd=float(flow_info.get("premium_usd", 0.0)),
            rank_cross=int(flow_info.get("rank_cross", 50)),
        ),
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


# ── Flow aggregation ─────────────────────────────────────────────────────────

def _aggregate_flow_per_ticker(flow_alerts_payload: dict, hot_15: list[str]) -> dict[str, dict]:
    """Group flow_alerts response by ticker → {alerts, premium_usd, spot, rank_cross}.

    UW flow-alerts row shape: {ticker, underlying_price, total_premium, ...}.
    rank_cross is derived from position in hot_15 (1st = top 7%, last = ~100%).
    """
    rows = flow_alerts_payload.get("data", flow_alerts_payload) if isinstance(flow_alerts_payload, dict) else flow_alerts_payload
    if not isinstance(rows, list):
        return {}
    agg: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = r.get("ticker") or r.get("ticker_symbol") or r.get("underlying")
        if not t:
            continue
        cur = agg.setdefault(t, {"alerts": 0, "premium_usd": 0.0, "spot": 0.0})
        cur["alerts"] += 1
        try:
            cur["premium_usd"] += float(r.get("total_premium") or 0)
        except (TypeError, ValueError):
            pass
        if cur["spot"] == 0.0:
            try:
                cur["spot"] = float(r.get("underlying_price") or 0)
            except (TypeError, ValueError):
                pass
    # Cross-sectional rank within hot_15 (top ticker = 7%, last = 100%)
    n = len(hot_15)
    for i, t in enumerate(hot_15):
        if t in agg:
            agg[t]["rank_cross"] = int(round((i + 1) / max(n, 1) * 100))
    return agg


# ── Extractors (use v1-tested parsers from server.uw) ────────────────────────

def _extract_oi(data: Any) -> list[dict]:
    """Top 5 strikes by total OI (call+put). UW oi-per-strike has call_oi + put_oi
    fields (today only). v0 doesn't compute Δ% — needs separate yesterday fetch."""
    if isinstance(data, storage.UWFailure) or not isinstance(data, dict):
        return []
    rows = data.get("data") or []
    enriched = []
    for r in rows:
        try:
            strike = float(r.get("strike") or 0)
            call_oi = int(r.get("call_oi") or 0)
            put_oi = int(r.get("put_oi") or 0)
            total = call_oi + put_oi
            if strike <= 0 or total <= 0:
                continue
            enriched.append({"strike": strike, "prev": total, "today": total, "pct": 0.0})
        except (TypeError, ValueError):
            continue
    enriched.sort(key=lambda x: -(x["today"]))
    return enriched[:5]


def _extract_iv_curve(data: Any) -> list[float]:
    """Use the v1 term_structure parser: returns sorted [{dte, iv}] from
    UW's 'volatility' field. We pick the first 4 expiries for the sparkline."""
    if isinstance(data, storage.UWFailure):
        return []
    term = uw.term_structure(data)
    return [round(e["iv"], 4) for e in term[:4]]


def _extract_ivr(ivr_data: Any, vol_data: Any) -> int:
    """v1 extract_iv_rank prefers interpolated-iv 'percentile' (0-1), * 100."""
    if isinstance(ivr_data, storage.UWFailure):
        ivr_data = None
    if isinstance(vol_data, storage.UWFailure):
        vol_data = None
    pct = uw.extract_iv_rank(vol_data, ivr_data)
    if pct is None:
        return 50
    return int(round(pct))


def _extract_darkpool(data: Any) -> DarkPool:
    """Sum price × size across recent prints for a rough net premium proxy.
    Sign convention: UW prints don't carry buy/sell side, so we just report
    total $ flow — actual signed net needs trade-by-trade classification."""
    if isinstance(data, storage.UWFailure) or not isinstance(data, dict):
        return DarkPool()
    rows = data.get("data") or []
    net = 0.0
    for r in rows:
        try:
            net += float(r.get("price") or 0) * int(r.get("size") or 0)
        except (TypeError, ValueError):
            continue
    trend = "buying" if net > 0 else "selling" if net < 0 else "neutral"
    return DarkPool(net_premium_usd=net, pct_of_volume=0, trend=trend)


def _extract_days_to_earnings(data: Any) -> int | None:
    """UW earnings endpoint: each row has report_date or start_date for an
    upcoming or historical event. Find the next future date."""
    if isinstance(data, storage.UWFailure) or not isinstance(data, dict):
        return None
    rows = data.get("data") or []
    today = datetime.now(tz=timezone.utc).date()
    candidates = []
    for r in rows:
        iso = r.get("report_date") or r.get("start_date") or r.get("date")
        if not iso:
            continue
        try:
            d = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).date()
            delta = (d - today).days
            if delta >= 0:
                candidates.append(delta)
        except (TypeError, ValueError):
            continue
    return min(candidates) if candidates else None


def _extract_gex(spot_data: Any, spot: float) -> tuple[float, float, float, str, float]:
    """Use v1 gex_records parser to extract per-strike net dealer gamma.
    Returns (flip_dist_pct, wall_up_dist_pct, wall_dn_dist_pct, gex_sign, agg_gamma_b).

    Flip = cumulative sum crosses zero. Wall = max |γ| strike on each side."""
    if isinstance(spot_data, storage.UWFailure) or spot <= 0:
        return 0.0, 5.0, 5.0, "POS", 0.0
    recs = uw.gex_records(spot_data)
    if not recs:
        return 0.0, 5.0, 5.0, "POS", 0.0
    # Cumulative sum from low strike → high; flip = first strike where cum changes sign
    recs_sorted = sorted(recs, key=lambda r: r["strike"])
    cum = 0.0
    flip = recs_sorted[0]["strike"]
    prev_sign = None
    for r in recs_sorted:
        cum += r["gamma"]
        sign = 1 if cum >= 0 else -1
        if prev_sign is not None and sign != prev_sign:
            flip = r["strike"]
            break
        prev_sign = sign
    above = [r for r in recs if r["strike"] > spot]
    below = [r for r in recs if r["strike"] < spot]
    wall_up = max(above, key=lambda r: abs(r["gamma"]), default=None)
    wall_dn = max(below, key=lambda r: abs(r["gamma"]), default=None)
    wall_up_strike = wall_up["strike"] if wall_up else spot * 1.05
    wall_dn_strike = wall_dn["strike"] if wall_dn else spot * 0.95
    agg = sum(r["gamma"] for r in recs) / 1e9
    return ((flip - spot) / spot * 100,
            (wall_up_strike - spot) / spot * 100,
            (spot - wall_dn_strike) / spot * 100,
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
