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
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

from server import gates, insights, storage, uw, universe
from server.schema import (DarkPool, ExpirySegment, Flow, FlowAlert, Insights,
                            NewsItem, OHLCBar, OI, OISessionBar, OIStrike, Regime,
                            Row, Snapshot, StrikeOIHistory, Tile2)

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

    # 1b. Cross-ticker ingest (parqueted, not yet consumed by tiles):
    # - lit_flow_recent: lit/dark divergence companion to darkpool
    # - group_flow (sp500): broad-market Greek-flow regime
    # - seasonality_market: 24h-cached market drift baseline
    # - net_flow_expiry (no ticker): cross-market expiry term-structure
    await asyncio.gather(
        loop.run_in_executor(_POOL, partial(storage.fetch_lit_flow_recent)),
        loop.run_in_executor(_POOL, partial(storage.fetch_group_flow, "sp500")),
        loop.run_in_executor(_POOL, partial(storage.fetch_seasonality_market)),
        loop.run_in_executor(_POOL, partial(storage.fetch_net_flow_expiry, None)),
        return_exceptions=True,
    )

    # 2. tracked_universe
    sticky = universe.StickyState(storage.load_sticky())
    sticky.touch(hot_15, now=now)
    sticky.decay(now=now)
    storage.save_sticky(sticky.to_dict())
    tracked = universe.compose_universe(hot_15=hot_15, sticky=sticky, now=now)

    # 3. Refresh archive for hot_15 only. Tracked-universe sleeper coverage
    # is disabled here while we debug rate-limit interactions — the broader
    # archive pass was generating 250+ extra calls per cycle and starving
    # flow_alerts. Re-enable once we confirm UW budget headroom in production.
    # tracked is still computed (for sticky maintenance) but not iterated.
    await asyncio.gather(*[
        _refresh_for_archive(t, is_hot=True, loop=loop)
        for t in hot_15
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
    # Prev-trading-day fetch for OI Δ% — use yesterday (calendar). UW typically
    # falls back to the last trading day if asked for a weekend, so this works
    # on Sat/Sun without market-calendar logic. Cached separately from today's.
    # Full ingest. With parquet read-through (commit caeda18) absorbing most
    # repeat calls, fetching broadly only spikes UW load on cold-cache cycles;
    # warm steady-state is mostly cache hits.
    yesterday_iso = (datetime.now(tz=timezone.utc).date() - timedelta(days=1)).isoformat()
    spot_data = await loop.run_in_executor(_POOL, partial(storage.fetch_spot_exposures_strike, ticker, is_hot))
    oi_data = await loop.run_in_executor(_POOL, partial(storage.fetch_oi_strike, ticker, is_hot))
    oi_prev_data = await loop.run_in_executor(_POOL, partial(storage.fetch_oi_strike, ticker, False, yesterday_iso))
    vol_data = await loop.run_in_executor(_POOL, partial(storage.fetch_volatility, ticker, is_hot))
    ivr_data = await loop.run_in_executor(_POOL, partial(storage.fetch_interpolated_iv, ticker, is_hot))
    dp_data = await loop.run_in_executor(_POOL, partial(storage.fetch_darkpool, ticker, is_hot))
    earn_data = await loop.run_in_executor(_POOL, partial(storage.fetch_earnings, ticker, is_hot))
    info_data = await loop.run_in_executor(_POOL, partial(storage.fetch_ticker_info, ticker))
    news_data = await loop.run_in_executor(_POOL, partial(storage.fetch_news_headlines, ticker, 10))
    # Ingest-only (not yet consumed by any tile — wires in a later UI pass):
    # option_contracts: real bid/ask/IV/vol/OI for Tile 6 picker
    # max_pain: additional Tile 4 context
    # net_prem_ticks: time-series flow context
    # net_flow_expiry: front-week-vs-30-45-DTE qualifier for flow alerts (per-ticker)
    # seasonality_ticker: 24h-cached per-ticker calendar drift baseline
    await loop.run_in_executor(_POOL, partial(storage.fetch_option_contracts, ticker, 500))
    await loop.run_in_executor(_POOL, partial(storage.fetch_max_pain, ticker))
    await loop.run_in_executor(_POOL, partial(storage.fetch_net_prem_ticks, ticker))
    await loop.run_in_executor(_POOL, partial(storage.fetch_net_flow_expiry, ticker))
    await loop.run_in_executor(_POOL, partial(storage.fetch_seasonality_ticker, ticker))
    # OHLC for Tile 1's price line. 5m candles match the 4hr session view.
    ohlc_data = await loop.run_in_executor(_POOL, partial(storage.fetch_ohlc, ticker, "5m", is_hot))

    failures = [r.endpoint for r in (spot_data, oi_data, vol_data, ivr_data, dp_data, earn_data)
                if isinstance(r, storage.UWFailure)]

    # Spot: prefer flow_alerts.underlying_price (live), then any payload root
    spot = flow_info.get("spot") or 0.0
    if not spot:
        spot = uw.extract_spot(spot_data, oi_data, ivr_data) or 0.0
    oi = _extract_oi(oi_data, oi_prev_data)
    iv_term = _extract_iv_curve(vol_data)
    ivr = _extract_ivr(ivr_data, vol_data)
    dp = _extract_darkpool(dp_data)
    days_to_earn = _extract_days_to_earnings(earn_data)
    flip_pct, wall_up_pct, wall_dn_pct, gex_sign, agg_b = _extract_gex(spot_data, spot)
    sector = _extract_sector(info_data, flow_info)
    news_items = _extract_news_items(news_data)
    flow_alerts_detail = _project_flow_alerts(flow_info.get("raw_alerts", []))
    ohlc_bars = _extract_ohlc(ohlc_data)
    ask_side_pct = float(flow_info.get("ask_side_pct", 0.0))
    # Tile 2: positioning reality check. 5-session OI from our own parquet
    # archive (tier-independent); per-strike delta from spot_exposures.
    oi_history = await loop.run_in_executor(_POOL, partial(storage.read_oi_history, ticker, 5))
    tile2 = _build_tile2(flow_alerts_detail, oi_history, spot_data, spot)
    # Chained sector-tide fetch once we know the sector slug. Skip if unknown.
    sector_tide_value = 0.0
    if sector:
        tide_data = await loop.run_in_executor(_POOL, partial(storage.fetch_sector_tide, sector))
        sector_tide_value = _extract_sector_tide_value(tide_data)

    # Direction inference: positive net dealer γ (gex_sign=POS) means dealers
    # are long γ → they sell into rallies, buy dips → suppresses upside
    # momentum. Negative γ means dealers are short γ → amplify moves in either
    # direction. For directional-trade framing we infer "calls" when γ is
    # negative (squeeze-friendly upside setup) or when spot is below γ flip
    # (room to grind higher into the flip), and "puts" otherwise.
    if gex_sign == "NEG" or flip_pct > 0:
        direction = "calls"
    else:
        direction = "puts"

    raw_row = {
        "ticker": ticker,
        "spot": spot,
        "direction": direction,
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
        direction=direction,
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
        sector=sector,
        sector_tide_value=sector_tide_value,
        dark_pool=dp,
        news_items=news_items,
        flow_alerts_detail=flow_alerts_detail,
        ohlc=ohlc_bars,
        ask_side_pct=ask_side_pct,
        tile2=tile2,
        _failures=failures,
    )


# ── Flow aggregation ─────────────────────────────────────────────────────────

def _aggregate_flow_per_ticker(flow_alerts_payload: dict, hot_15: list[str]) -> dict[str, dict]:
    """Group flow_alerts response by ticker → {alerts, premium_usd, spot,
    rank_cross, raw_alerts, ask_side_pct}.

    UW flow-alerts row shape: {ticker, underlying_price, total_premium,
    total_ask_side_prem, total_bid_side_prem, has_sweep, has_singleleg, ...}.
    rank_cross is derived from cross-sectional premium percentile across all
    tickers in the response. raw_alerts preserves the per-alert detail Tile 1
    plots as bubbles."""
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
        cur = agg.setdefault(t, {"alerts": 0, "premium_usd": 0.0, "spot": 0.0,
                                  "ask_sum": 0.0, "bid_sum": 0.0, "raw_alerts": []})
        cur["alerts"] += 1
        try:
            cur["premium_usd"] += float(r.get("total_premium") or 0)
        except (TypeError, ValueError):
            pass
        try:
            cur["ask_sum"] += float(r.get("total_ask_side_prem") or 0)
        except (TypeError, ValueError):
            pass
        try:
            cur["bid_sum"] += float(r.get("total_bid_side_prem") or 0)
        except (TypeError, ValueError):
            pass
        if cur["spot"] == 0.0:
            try:
                cur["spot"] = float(r.get("underlying_price") or 0)
            except (TypeError, ValueError):
                pass
        cur["raw_alerts"].append(r)
    # Cross-sectional rank: order ALL tickers in the flow_alerts response by
    # total premium descending, then map percentile. Top ~7% of tickers in
    # today's universe = green; top ~35% = yellow.
    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["premium_usd"])
    total_n = max(len(ranked), 1)
    for i, (t, info) in enumerate(ranked):
        info["rank_cross"] = int(round((i + 1) / total_n * 100))
        ask_bid = info["ask_sum"] + info["bid_sum"]
        info["ask_side_pct"] = (info["ask_sum"] / ask_bid) if ask_bid > 0 else 0.0
    return agg


# ── Extractors (use v1-tested parsers from server.uw) ────────────────────────

def _project_flow_alerts(raw_alerts: list[dict]) -> list[FlowAlert]:
    """Project UW flow_alerts rows (string-typed numerics) onto FlowAlert
    models — Tile 1's scatter consumes this. Skips multileg in line with the
    Tile 1 build spec's default-view rule (single-leg only)."""
    out: list[FlowAlert] = []
    for r in raw_alerts:
        if not isinstance(r, dict):
            continue
        try:
            if not bool(r.get("has_singleleg", False)):
                continue
            t = r.get("type") or ""
            if t not in ("call", "put"):
                continue
            out.append(FlowAlert(
                created_at=str(r.get("created_at") or ""),
                strike=float(r.get("strike") or 0),
                type=t,
                total_premium=float(r.get("total_premium") or 0),
                total_ask_side_prem=float(r.get("total_ask_side_prem") or 0),
                total_bid_side_prem=float(r.get("total_bid_side_prem") or 0),
                has_sweep=bool(r.get("has_sweep", False)),
                has_singleleg=bool(r.get("has_singleleg", False)),
                has_multileg=bool(r.get("has_multileg", False)),
                underlying_price=float(r.get("underlying_price") or 0),
                option_chain=str(r.get("option_chain") or ""),
                expiry=str(r.get("expiry") or ""),
                total_size=int(float(r.get("total_size") or 0)),
                all_opening_trades=bool(r.get("all_opening_trades", False)),
                volume_oi_ratio=float(r.get("volume_oi_ratio") or 0),
                volume=int(float(r.get("volume") or 0)),
                open_interest=int(float(r.get("open_interest") or 0)),
            ))
        except (TypeError, ValueError):
            continue
    return out


def _per_strike_net_delta(spot_data: Any) -> dict[float, float]:
    """Map strike → net delta (call_delta_oi + put_delta_oi) from the
    greek-exposure (spot-exposures/strike) payload. Spec: delta sourced from
    greek-exposure endpoint ONLY."""
    out: dict[float, float] = {}
    if isinstance(spot_data, storage.UWFailure) or not isinstance(spot_data, dict):
        return out
    for r in (spot_data.get("data") or []):
        try:
            k = float(r.get("strike") or 0)
            if k <= 0:
                continue
            cd = float(r.get("call_delta_oi") or 0)
            pd = float(r.get("put_delta_oi") or 0)
            out[k] = cd + pd
        except (TypeError, ValueError):
            continue
    return out


def _nearest_delta(delta_by_strike: dict[float, float], k: float, tol_pct: float = 1.5) -> float:
    """Greek-exposure strikes are gridded (e.g. $5 spacing) and rarely match a
    flow strike exactly. Match to the nearest greek strike within tol_pct of k."""
    if not delta_by_strike or k <= 0:
        return 0.0
    if k in delta_by_strike:
        return delta_by_strike[k]
    best = min(delta_by_strike, key=lambda x: abs(x - k))
    return delta_by_strike[best] if abs(best - k) / k * 100 <= tol_pct else 0.0


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _build_tile2(flow_alerts: list[FlowAlert], oi_history: list[dict],
                 spot_data: Any, spot: float) -> Tile2:
    """Assemble Tile 2's positioning-reality model from:
      - flow_alerts: opening read (all_opening_trades, volume_oi_ratio) +
        per-strike premium + expiry distribution
      - oi_history: oldest→newest daily OI snapshots from our parquet archive
      - spot_data: per-strike net delta (greek-exposure)
    """
    # ── Opening read ──────────────────────────────────────────────────────
    n = len(flow_alerts)
    opening_n = sum(1 for a in flow_alerts if a.all_opening_trades)
    opening_pct = (opening_n / n * 100) if n else 0.0
    # Median, not mean — a single brand-new far-OTM strike (tiny prior OI) can
    # blow a mean vol/OI ratio into the hundreds and misrepresent the batch.
    vois = [a.volume_oi_ratio for a in flow_alerts if a.volume_oi_ratio > 0]
    med_voi = _median(vois)

    # ── Per-strike premium (flow concentration) + today vol/OI ────────────
    prem_by_strike: dict[float, float] = {}
    voi_by_strike: dict[float, list] = {}
    for a in flow_alerts:
        if a.strike > 0:
            prem_by_strike[a.strike] = prem_by_strike.get(a.strike, 0.0) + a.total_premium
            if a.volume_oi_ratio > 0:
                voi_by_strike.setdefault(a.strike, []).append(a.volume_oi_ratio)

    # ── Expiry distribution (premium grouped by expiry) ───────────────────
    prem_by_expiry: dict[str, float] = {}
    for a in flow_alerts:
        if a.expiry:
            prem_by_expiry[a.expiry] = prem_by_expiry.get(a.expiry, 0.0) + a.total_premium
    total_prem = sum(prem_by_expiry.values())
    expiry_dist = [
        ExpirySegment(expiry=e, premium_usd=p,
                      pct=round(p / total_prem * 100, 1) if total_prem else 0.0)
        for e, p in sorted(prem_by_expiry.items(), key=lambda kv: -kv[1])
    ]

    # ── Per-strike OI history (from parquet archive) ──────────────────────
    delta_by_strike = _per_strike_net_delta(spot_data)
    # Focus on the strikes the flow actually targets (top 6 by premium), so the
    # chart isn't cluttered with deep-OTM index OI. Fall back to most-recent-
    # session top-OI strikes when flow carries no strike premium.
    if prem_by_strike:
        focus = sorted(prem_by_strike, key=lambda k: -prem_by_strike[k])[:6]
    elif oi_history:
        latest = oi_history[-1]["strikes"]
        focus = sorted(latest, key=lambda k: -latest[k])[:6]
    else:
        focus = []

    # Spec: today's OI is NOT settled until ~9am next session, so it is shown
    # as a live vol/OI ratio — never as a settled bar. Treat the most-recent
    # archived session as "today" (provisional) and bar only the settled days.
    today_date = datetime.now(tz=timezone.utc).date().isoformat()
    settled = [s for s in oi_history if s["date"] != today_date]
    settled_dates = [s["date"] for s in settled]

    strike_hist: list[StrikeOIHistory] = []
    for k in sorted(focus):
        bars = [OISessionBar(date=s["date"], oi=int(s["strikes"].get(k, 0)),
                             provisional=False)
                for s in settled]
        # delta_oi = newest settled vs prior settled
        delta_oi = 0
        if len(bars) >= 2 and bars[-2].oi > 0:
            delta_oi = bars[-1].oi - bars[-2].oi
            ratio = delta_oi / bars[-2].oi
            trend = "building" if ratio >= 0.05 else "unwinding" if ratio <= -0.05 else "flat"
        else:
            trend = "flat"
        voi_list = voi_by_strike.get(k, [])
        strike_hist.append(StrikeOIHistory(
            strike=k,
            sessions=bars,
            delta_oi=delta_oi,
            net_delta=_nearest_delta(delta_by_strike, k),
            premium_usd=prem_by_strike.get(k, 0.0),
            trend=trend,
            today_vol_oi=round(_median(voi_list), 2),
        ))

    # ── Aggregate OI trend across focus strikes (settled days only) ───────
    n_settled = len(settled)
    oi_trend_5d_pct = 0.0
    if n_settled >= 2 and strike_hist:
        first_total = sum(s.sessions[0].oi for s in strike_hist if s.sessions)
        last_total = sum(s.sessions[-1].oi for s in strike_hist if s.sessions)
        if first_total > 0:
            oi_trend_5d_pct = round((last_total - first_total) / first_total * 100, 1)

    # ── Confirmation state ────────────────────────────────────────────────
    if n_settled < 2:
        confirmation = "unconfirmed"   # not enough settled archive history yet
    elif oi_trend_5d_pct >= 5:
        confirmation = "building"
    elif oi_trend_5d_pct <= -5:
        confirmation = "unwinding"
    else:
        confirmation = "flat"

    # ── Low-conviction state ──────────────────────────────────────────────
    n_expiries = len(prem_by_expiry)
    n_strikes = len(prem_by_strike)
    low_conviction = n > 0 and (n_expiries >= 4 or n_strikes >= 8) and \
        (max(prem_by_expiry.values(), default=0) / total_prem if total_prem else 0) < 0.4
    low_msg = (f"Flow scattered across {n_expiries} expirations / {n_strikes} strikes "
               f"— no clear target.") if low_conviction else ""

    return Tile2(
        opening_pct=round(opening_pct, 1),
        avg_volume_oi_ratio=round(med_voi, 2),
        oi_trend_5d_pct=oi_trend_5d_pct,
        confirmation=confirmation,
        sessions_available=n_settled,   # settled days that actually bar
        strikes=strike_hist,
        expiry_distribution=expiry_dist,
        low_conviction=low_conviction,
        low_conviction_msg=low_msg,
    )


def _extract_ohlc(ohlc_data: Any) -> list[OHLCBar]:
    """Map UW OHLC payload → OHLCBar list. UW timestamp is usually ISO; we
    convert to epoch-seconds so the JS chart code doesn't have to parse."""
    if isinstance(ohlc_data, storage.UWFailure) or not isinstance(ohlc_data, dict):
        return []
    rows = ohlc_data.get("data") or []
    bars: list[OHLCBar] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            ts_raw = r.get("start_time") or r.get("t") or r.get("timestamp") or ""
            if isinstance(ts_raw, (int, float)):
                t_epoch = int(ts_raw)
            else:
                dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                t_epoch = int(dt.timestamp())
            bars.append(OHLCBar(
                t=t_epoch,
                o=float(r.get("open") or r.get("o") or 0),
                h=float(r.get("high") or r.get("h") or 0),
                l=float(r.get("low") or r.get("l") or 0),
                c=float(r.get("close") or r.get("c") or 0),
                v=float(r.get("volume") or r.get("v") or 0),
            ))
        except (TypeError, ValueError):
            continue
    # Trim to the most recent 4-hour window (5m candles = 48 bars max).
    bars.sort(key=lambda b: b.t)
    return bars[-48:]


def _extract_oi(today_data: Any, prev_data: Any) -> list[dict]:
    """Top 5 strikes by today's total OI (call+put), with Δ% computed against
    yesterday's snapshot. UW oi-per-strike has call_oi + put_oi fields.

    Pct semantics match the gates layer: ≥20% green (opening), ≥5% yellow,
    ≤-5% red (closing). Missing prev data → pct=0 (marginal)."""
    if isinstance(today_data, storage.UWFailure) or not isinstance(today_data, dict):
        return []
    today_rows = today_data.get("data") or []

    # Build prev lookup: strike → total OI on yesterday (or empty if prev fetch failed)
    prev_by_strike: dict[float, int] = {}
    if isinstance(prev_data, dict):
        for r in prev_data.get("data") or []:
            try:
                strike = float(r.get("strike") or 0)
                prev_total = int(r.get("call_oi") or 0) + int(r.get("put_oi") or 0)
                if strike > 0:
                    prev_by_strike[strike] = prev_total
            except (TypeError, ValueError):
                continue

    enriched = []
    for r in today_rows:
        try:
            strike = float(r.get("strike") or 0)
            call_oi = int(r.get("call_oi") or 0)
            put_oi = int(r.get("put_oi") or 0)
            today_total = call_oi + put_oi
            if strike <= 0 or today_total <= 0:
                continue
            prev_total = prev_by_strike.get(strike, today_total)
            pct = round(((today_total - prev_total) / prev_total * 100) if prev_total else 0.0, 1)
            enriched.append({"strike": strike, "prev": prev_total, "today": today_total, "pct": pct})
        except (TypeError, ValueError):
            continue
    # Sort by |pct| descending so the most-changed strikes float to the top
    enriched.sort(key=lambda x: -abs(x["pct"]))
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


def _extract_sector(info_data: Any, flow_info: dict) -> str:
    """Resolve a sector slug for the ticker. Prefer the ticker_info endpoint;
    fall back to whatever sector was on the flow_alerts row (often null for
    indices). Return empty string when unknown — Tile 5 handles that case."""
    if isinstance(info_data, dict):
        rows = info_data.get("data")
        first = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else {})
        sec = (first.get("sector") if isinstance(first, dict) else None)
        if sec:
            return str(sec).strip().lower().replace(" ", "-")
    sec2 = flow_info.get("sector")
    return str(sec2).strip().lower().replace(" ", "-") if sec2 else ""


def _extract_news_items(data: Any) -> list[NewsItem]:
    """Up to 5 most-recent headlines. UW news row shape varies but commonly
    includes {headline|title, source|publisher, published_at|date|time}."""
    if isinstance(data, storage.UWFailure) or not isinstance(data, dict):
        return []
    rows = data.get("data") or []
    out: list[NewsItem] = []
    for r in rows[:5]:
        try:
            headline = r.get("headline") or r.get("title") or ""
            source = r.get("source") or r.get("publisher") or "UW"
            ts = r.get("published_at") or r.get("date") or r.get("time") or ""
            # If ts is an ISO datetime, take HH:MM; if already short, keep as-is.
            time_str = str(ts)[11:16] if "T" in str(ts) and len(str(ts)) > 15 else str(ts)[:5]
            if headline:
                out.append(NewsItem(time=time_str, source=str(source)[:20], headline=str(headline)[:200]))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _extract_sector_tide_value(data: Any) -> float:
    """Sector tide is a time-series; we want the most recent net direction
    normalized to roughly [-1, +1]. UW sector-tide rows commonly have
    `net_call_premium`, `net_put_premium`, or a derived `tide` field.
    Return 0.0 on missing data."""
    if isinstance(data, storage.UWFailure) or not isinstance(data, dict):
        return 0.0
    rows = data.get("data") or []
    if not rows:
        return 0.0
    # Take latest (last entry) — UW typically returns chronological order
    latest = rows[-1] if isinstance(rows[-1], dict) else {}
    # Try a few shapes
    direct = latest.get("tide")
    if direct is not None:
        try:
            return max(-1.0, min(1.0, float(direct)))
        except (TypeError, ValueError):
            pass
    try:
        ncp = float(latest.get("net_call_premium") or 0)
        npp = float(latest.get("net_put_premium") or 0)
        net = ncp - npp
        gross = abs(ncp) + abs(npp)
        return round(net / gross, 2) if gross else 0.0
    except (TypeError, ValueError):
        return 0.0


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
