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
import contextvars
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, datetime, timedelta, timezone
from functools import partial
from typing import Any

from server import gates, gex, insights, market_regime, storage, uw, universe
from server import verdict as verdict_mod
from server.schema import (DarkPool, ExpirySegment, Flow, FlowAlert, Insights,
                            NewsItem, OHLCBar, OI, OISessionBar, OIStrike, Regime,
                            Row, Snapshot, StrikeOIHistory, Tile2, Tile3, Tile3Strike,
                            Verdict)

log = logging.getLogger(__name__)

_POOL = ThreadPoolExecutor(max_workers=8)


def _in_ctx(loop, fn):
    """Submit `fn` to the shared pool, carrying the CALLER's contextvars into the
    worker thread. asyncio's run_in_executor does NOT propagate context, so the
    request path's `storage.cached_only()` guard and the freshness collector
    (both contextvars) would be invisible inside pool threads without this — the
    bug that made replay search-any-ticker fetch live (degraded) and dropped
    lookup's freshness stamp. copy_context() snapshots the vars here (in the
    event-loop thread, where they're set); ctx.run executes fn with them in the
    pool. Returns the same awaitable as run_in_executor."""
    ctx = contextvars.copy_context()
    return loop.run_in_executor(_POOL, lambda: ctx.run(fn))


def _regime_num(payload, *keys):
    """First numeric value found under any of `keys` in payload['data'][0]."""
    if isinstance(payload, storage.UWFailure) or not isinstance(payload, dict):
        return None
    rows = payload.get("data") or []
    if not rows or not isinstance(rows[0], dict):
        return None
    for k in keys:
        v = rows[0].get(k)
        try:
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _regime_vol(payload):
    """(iv, rv) for the regime, from /volatility/realized — a daily IV+RV series
    (oldest→newest). iv = the latest row's implied_volatility (today's, populated
    even before today's RV settles); rv = the latest row with a non-null
    realized_volatility (today's RV settles next session). Both decimal annualized.
    (None, None) on failure/empty. This is a matched IV/RV pair from ONE endpoint —
    replaces the spiky interpolated_iv front-week (1DTE) read, and drops a fetch."""
    if isinstance(payload, storage.UWFailure) or not isinstance(payload, dict):
        return (None, None)
    rows = [r for r in (payload.get("data") or []) if isinstance(r, dict)]
    if not rows:
        return (None, None)

    def _f(x):
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None

    iv = _f(rows[-1].get("implied_volatility"))
    rv = None
    for r in reversed(rows):
        rv = _f(r.get("realized_volatility"))
        if rv is not None:
            break
    return (iv, rv)


def _regime_events(payload):
    if isinstance(payload, storage.UWFailure) or not isinstance(payload, dict):
        return []
    return [e for e in (payload.get("data") or []) if isinstance(e, dict)]


def _regime_tide_lean(payload):
    net = _regime_num(payload, "net_call_premium", "net_premium", "tide", "net_flow")
    if net is None:
        return "neutral"
    return "bull" if net > 0 else "bear" if net < 0 else "neutral"


def _is_opex_week(d) -> bool:
    """True during the week containing the 3rd Friday of the month (Mon-Fri)."""
    import calendar
    fridays = [day for week in calendar.monthcalendar(d.year, d.month)
               for day in (week[calendar.FRIDAY],) if day]
    if len(fridays) < 3:
        return False
    third = fridays[2]
    from datetime import date
    monday = third - date(d.year, d.month, third).weekday()
    return monday <= d.day <= third


def _skew_expiry(d) -> str:
    """3rd-Friday monthly expiry >= ~25 DTE out — the ~30d skew horizon (less noisy
    than the weekly wings). Monthlies exist for any liquid optionable name; a name
    without it → greeks 404 → skew 'unavailable' (graceful)."""
    import calendar
    from datetime import date, timedelta

    def third_friday(y, m):
        first = date(y, m, 1)
        offset = (calendar.FRIDAY - first.weekday()) % 7
        return first + timedelta(days=offset + 14)

    cand = third_friday(d.year, d.month)
    if (cand - d).days < 25:
        ny, nm = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        cand = third_friday(ny, nm)
    return cand.isoformat()


async def _macro_event_within_hold(loop) -> bool:
    econ = await _in_ctx(loop, partial(storage.fetch_economic_calendar))
    return market_regime._next_event(_regime_events(econ), datetime.now(tz=timezone.utc)) is not None


async def _build_market_regime(loop):
    """Returns (Regime, event_within_hold). Honest-degrades: any missing input
    yields a conservative regime, never a fabricated 'Favorable'."""
    now = datetime.now(tz=timezone.utc)
    spy_state = await _in_ctx(loop, partial(storage.fetch_stock_state, "SPY"))
    spy_spot = uw.extract_spot(spy_state) or 0.0
    gmin = round(spy_spot * 0.80) if spy_spot else None
    gmax = round(spy_spot * 1.20) if spy_spot else None
    spy_gex = await _in_ctx(loop, partial(storage.fetch_spot_exposures_strike, "SPY", True, gmin, gmax))
    flip, _wu, _wd, sign, _agg, status = _extract_gex(spy_gex, spy_spot)
    iv, rv = _regime_vol(await _in_ctx(loop, partial(storage.fetch_realized_vol, "SPY")))
    econ = await _in_ctx(loop, partial(storage.fetch_economic_calendar))
    tide = await _in_ctx(loop, partial(storage.fetch_market_tide))
    tide_val = _regime_num(tide, "net_call_premium", "net_premium", "tide", "net_flow")
    reg = market_regime.compute_market_regime(
        gamma={"sign": sign, "flip_pct": flip, "status": status},
        vol={"iv": iv, "rv": rv, "trend": None},
        events=_regime_events(econ),
        tide={"lean": _regime_tide_lean(tide)},
        opex=_is_opex_week(now.date()),
        now=now)
    label = os.environ.get("REGIME", "normal").lower()
    if label not in ("normal", "risk-off"):
        label = "normal"
    gamma_ok = status == "ok" and sign in ("POS", "NEG")
    regime = Regime(label=label,
                    headline=reg["headline"], posture=reg["posture"],
                    event_line=reg["event"]["line"], vol_line=reg["vol"],
                    tide_badge=reg["tide_badge"], opex=reg["opex"],
                    as_of=now.isoformat(),
                    # evidence behind the posture (only the trustworthy reads)
                    spy_spot=spy_spot or None,
                    flip_pct=flip if gamma_ok else None,
                    gex_sign=sign if gamma_ok else "",
                    iv=iv, rv=rv, tide_value=tide_val)
    return regime, reg["event_within_hold"]


async def refresh_snapshot() -> Snapshot:
    """One full cycle. Returns the freshly-built Snapshot."""
    loop = asyncio.get_running_loop()
    now = datetime.now(tz=timezone.utc)

    # 1. hot_15 from flow-alerts
    flow_alerts = await _in_ctx(loop, partial(storage.fetch_flow_alerts, 100))
    if isinstance(flow_alerts, storage.UWFailure):
        log.error("flow-alerts fetch failed: %s", flow_alerts.message)
        return _empty_snapshot(now)
    hot_15 = universe.top_15_unique_tickers(flow_alerts)
    flow_by_ticker = _aggregate_flow_per_ticker(flow_alerts, hot_15)

    # 1b. Cross-ticker ingest (lit_flow_recent, group_flow, seasonality_market,
    # net_flow_expiry) was fetched here to seed the parquet archive, but NO tile
    # consumes it yet. Each cycle it spent 4 UW calls (2 of them hot, refetched
    # every cycle) against the 120/min · 40k/day Basic budget — part of the 429
    # cascade behind the 2026-05-29 blackout. Dropped until a consuming UI ships;
    # re-add the gather here (and the per-ticker block in _build_dashboard_row)
    # when that lands.

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
    regime_obj, event_flag = await _build_market_regime(loop)
    rows = []
    for ticker in hot_15:
        flow_info = flow_by_ticker.get(ticker, {"alerts": 0, "premium_usd": 0.0,
                                                 "rank_cross": 50, "spot": 0.0})
        row = await _build_dashboard_row(ticker, flow_info=flow_info, loop=loop,
                                         event_within_hold=event_flag)
        rows.append(row)

    # NOTE: an earlier rework pre-warmed the Tile 3-detail + Tile 4 picker for all
    # hot_15 here, every cycle. That added ~120 UW calls/cycle and blew the daily
    # budget (2026-06-01). Reverted to ON-DEMAND: /api/tile3 and /api/tile4 fetch
    # live when a ticker is selected (a human views 2-3, not 15) — far cheaper.
    # The cost of all-15 prewarm wasn't worth it for a single-operator app.

    # 5. Insights
    for row in rows:
        row.insights = Insights(**insights.generate_insights(row.model_dump()))

    # 6. Assemble + persist
    snap = Snapshot(
        fetched_at=now,
        regime=regime_obj,
        rows=rows,
    )
    storage.append_snapshot(snap.model_dump(mode="json"))
    log.info("snapshot built: hot=%d tracked=%d", len(hot_15), len(tracked))
    return snap


def _light_row(ticker: str, flow_info: dict) -> Row:
    """A flow-only grid row: flow gate + flow-derived direction, no per-ticker
    gamma/OI/IV. The other three gates are placeholders (the frontend renders them
    'click to evaluate' whenever is_light is set, so the stored value is unused).
    direction/direction_basis come straight from derive_direction (schema-valid);
    a light row with basis 'gamma_fallback' means flow was ambiguous AND we fetched
    no gamma — the frontend shows that as a provisional 'dir on click', not a side."""
    flow_alerts_detail = _project_flow_alerts(flow_info.get("raw_alerts", []))
    direction, basis = gates.derive_direction(flow_alerts_detail, "", 0.0)
    flow_gate = gates.compute_gates({"flow_rank_cross": flow_info.get("rank_cross", 50)},
                                    history=None)["flow"]
    return Row(
        ticker=ticker,
        spot=float(flow_info.get("spot") or 0.0),
        direction=direction,
        direction_basis=basis,
        is_synthetic=True,
        is_light=True,
        gates={"flow": flow_gate, "oi": "yellow", "structural": "yellow", "cost": "yellow"},
        gate_method={"flow": "cross_sectional", "oi": "absolute",
                     "structural": "absolute", "cost": "percentile"},
        flow=Flow(
            alerts=int(flow_info.get("alerts", 0)),
            premium_usd=float(flow_info.get("premium_usd", 0.0)),
            rank_cross=int(flow_info.get("rank_cross", 50)),
        ),
        flow_alerts_detail=flow_alerts_detail,
        ask_side_pct=float(flow_info.get("ask_side_pct", 0.0)),
    )


async def build_light_snapshot() -> Snapshot:
    """Flow-only grid: one flow_alerts call → hot-15 ranked light rows. No
    per-ticker heavy endpoints, no regime (the front door attaches that on its own
    TTL). Used by get_or_build_snapshot for cheap page-load builds."""
    loop = asyncio.get_running_loop()
    now = datetime.now(tz=timezone.utc)
    flow_alerts = await _in_ctx(loop, partial(storage.fetch_flow_alerts, 100))
    if isinstance(flow_alerts, storage.UWFailure):
        log.error("light build: flow-alerts failed: %s", flow_alerts.message)
        return _empty_snapshot(now)
    hot_15 = universe.top_15_unique_tickers(flow_alerts)
    flow_by_ticker = _aggregate_flow_per_ticker(flow_alerts, hot_15)
    rows = [
        _light_row(t, flow_by_ticker.get(t, {"alerts": 0, "premium_usd": 0.0,
                                             "rank_cross": 50, "spot": 0.0}))
        for t in hot_15
    ]
    return Snapshot(fetched_at=now, regime=_current_regime(), rows=rows)


_RAM: dict = {"latest": None}
_SNAPSHOT_MAX_AGE_S = int(os.environ.get("SNAPSHOT_MAX_AGE_S", "60"))   # flow grid TTL
_REGIME_MAX_AGE_S = int(os.environ.get("REGIME_MAX_AGE_S", "600"))      # regime TTL


def _age_s(iso_or_dt, now: datetime) -> float:
    """Seconds between `iso_or_dt` (ISO string or datetime) and `now`. Treats
    missing/unparseable as effectively infinite (forces a rebuild)."""
    if iso_or_dt is None:
        return 1e9
    if isinstance(iso_or_dt, str):
        try:
            t = datetime.fromisoformat(iso_or_dt.replace("Z", "+00:00"))
        except ValueError:
            return 1e9
    else:
        t = iso_or_dt
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds()


async def get_or_build_snapshot(*, force_flow: bool = False) -> Snapshot:
    """Cache-or-build front door (request-driven; no background loop). Per-namespace
    freshness: rebuild the flow grid when older than SNAPSHOT_MAX_AGE_S (or when
    force_flow), and recompute the regime only when older than REGIME_MAX_AGE_S —
    otherwise carry the cached regime forward onto the rebuilt grid. Persists every
    build (append-only). Never overwrites a good build with an empty one."""
    now = datetime.now(tz=timezone.utc)
    cached = _RAM.get("latest")
    if cached is None:
        raw = storage.read_last_snapshot()
        if raw:
            try:
                cached = Snapshot.model_validate(raw)
                _RAM["latest"] = cached
            except Exception:
                cached = None

    flow_fresh = (not force_flow) and cached is not None and bool(cached.rows) and \
        _age_s(cached.fetched_at, now) < _SNAPSHOT_MAX_AGE_S
    if flow_fresh:
        return cached

    fresh = await build_light_snapshot()
    if not fresh.rows and cached is not None and cached.rows:
        return cached   # upstream flow failed — keep last good, don't blank

    regime_fresh = cached is not None and getattr(cached.regime, "posture", "") and \
        _age_s(getattr(cached.regime, "as_of", None), now) < _REGIME_MAX_AGE_S
    if regime_fresh:
        fresh.regime = cached.regime           # carry forward — 0 SPY calls
    else:
        loop = asyncio.get_running_loop()
        regime_obj, _event = await _build_market_regime(loop)
        if regime_obj is not None:
            fresh.regime = regime_obj

    _RAM["latest"] = fresh
    storage.append_snapshot(fresh.model_dump(mode="json"))
    return fresh


async def build_single_row(ticker: str) -> Row | None:
    """Build a full dashboard row for ANY ticker on demand (search-any-ticker).
    Reuses _build_dashboard_row — the same row the hot-15 get. Fetches the
    ticker's own flow first (a non-hot ticker may have none → flow_info zeros,
    Tile 1 quiet). Returns None only if the row build itself fails."""
    ticker = ticker.upper()
    loop = asyncio.get_running_loop()
    # Per-ticker deep-dive: pull 200 (one call — limit is a page size, not extra
    # budget) so Tile 1 covers more of the day for very active names. Quiet names
    # have far fewer alerts than this, so they're unaffected.
    flow_alerts = await _in_ctx(loop, partial(storage.fetch_flow_alerts, 200, ticker))
    if isinstance(flow_alerts, storage.UWFailure):
        flow_by_ticker = {}
    else:
        flow_by_ticker = _aggregate_flow_per_ticker(flow_alerts, [ticker])
    flow_info = flow_by_ticker.get(ticker, {"alerts": 0, "premium_usd": 0.0,
                                            "rank_cross": 50, "spot": 0.0})
    # Tile 2's 5-session OI is read from OUR archive (not a live UW call), so a
    # ticker we've never tracked has no history. Backfill its recent OI from UW
    # (idempotent — skips days already archived) so Tile 2 shows a real campaign.
    # Best-effort; runs before _build_dashboard_row reads the history. SKIPPED in
    # cached-only/replay mode — backfill calls UW directly (bypasses cached_only)
    # so it must not fire when we're meant to read the archive only.
    if not storage._CACHED_ONLY.get():
        try:
            from server import backfill
            await _in_ctx(loop, partial(backfill.backfill_oi_history, [ticker], 7))
        except Exception as e:
            log.warning("OI backfill for lookup %s failed (non-fatal): %s", ticker, e)
    event_flag = await _macro_event_within_hold(loop)
    row = await _build_dashboard_row(ticker, flow_info=flow_info, loop=loop,
                                     event_within_hold=event_flag)
    row.insights = Insights(**insights.generate_insights(row.model_dump()))
    # Deep-dive 3-leg verdict (Plan 3). Skew RR25 at a ~30d expiry: PRIMARY = UW's
    # vendor historical-risk-reversal-skew (clean, sign-corrected in verdict_mod);
    # FALLBACK = derived from greeks (call-25Δ IV − put-25Δ IV) when vendor is
    # unavailable. Either missing → skew 'unavailable'; verdict still computes.
    skew_exp = _skew_expiry(datetime.now(tz=timezone.utc).date())
    rr25 = None
    try:
        rr_payload = await _in_ctx(loop, partial(storage.fetch_risk_reversal_skew, ticker, skew_exp, 25))
        rr25 = verdict_mod.extract_vendor_rr(rr_payload)
    except Exception as e:
        log.warning("verdict vendor-skew fetch failed for %s: %s", ticker, e)
    if rr25 is None:
        try:
            gx = await _in_ctx(loop, partial(storage.fetch_greeks, ticker, skew_exp))
            gx_rows = gx.get("data") if isinstance(gx, dict) else None
            rr25 = verdict_mod.derive_rr25(gx_rows or [])
        except Exception as e:
            log.warning("verdict derived-skew fallback failed for %s: %s", ticker, e)
    row.verdict = Verdict(**verdict_mod.compute_verdict(
        direction=row.direction, direction_basis=row.direction_basis,
        flow_gate=row.gates.flow, structural_gate=row.gates.structural,
        oi_confirmation=row.tile2.confirmation, rr25=rr25, cost_gate=row.gates.cost))
    return row


async def _refresh_for_archive(ticker: str, *, is_hot: bool, loop):
    """Fetch all per-ticker endpoints; storage.fetch_* writes parquet on cache miss."""
    tasks = [
        _in_ctx(loop, partial(storage.fetch_spot_exposures_strike, ticker, is_hot)),
        _in_ctx(loop, partial(storage.fetch_oi_strike, ticker, is_hot)),
        _in_ctx(loop, partial(storage.fetch_volatility, ticker, is_hot)),
        _in_ctx(loop, partial(storage.fetch_interpolated_iv, ticker, is_hot)),
        _in_ctx(loop, partial(storage.fetch_darkpool, ticker, is_hot)),
        _in_ctx(loop, partial(storage.fetch_earnings, ticker, is_hot)),
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _build_dashboard_row(ticker: str, *, flow_info: dict, loop, event_within_hold: bool = False) -> Row:
    is_hot = True
    # Prev-trading-day fetch for OI Δ% — use yesterday (calendar). UW typically
    # falls back to the last trading day if asked for a weekend, so this works
    # on Sat/Sun without market-calendar logic. Cached separately from today's.
    # Full ingest. With parquet read-through (commit caeda18) absorbing most
    # repeat calls, fetching broadly only spikes UW load on cold-cache cycles;
    # warm steady-state is mostly cache hits.
    yesterday_iso = (datetime.now(tz=timezone.utc).date() - timedelta(days=1)).isoformat()
    # Spot-exposures must be requested as a near-spot strike window (±20% of the
    # flow spot). Without min/max_strike UW returns the lowest strikes in the
    # chain — far below spot for high-priced names — which made the gamma flip
    # and walls garbage. Rounded so small spot jitter doesn't bust the cache key.
    flow_spot = float(flow_info.get("spot") or 0)
    gex_min = round(flow_spot * 0.80) if flow_spot > 0 else None
    gex_max = round(flow_spot * 1.20) if flow_spot > 0 else None
    spot_data = await _in_ctx(loop, partial(
        storage.fetch_spot_exposures_strike, ticker, is_hot, gex_min, gex_max))
    oi_data = await _in_ctx(loop, partial(storage.fetch_oi_strike, ticker, is_hot))
    oi_prev_data = await _in_ctx(loop, partial(storage.fetch_oi_strike, ticker, False, yesterday_iso))
    vol_data = await _in_ctx(loop, partial(storage.fetch_volatility, ticker, is_hot))
    ivr_data = await _in_ctx(loop, partial(storage.fetch_interpolated_iv, ticker, is_hot))
    dp_data = await _in_ctx(loop, partial(storage.fetch_darkpool, ticker, is_hot))
    earn_data = await _in_ctx(loop, partial(storage.fetch_earnings, ticker, is_hot))
    info_data = await _in_ctx(loop, partial(storage.fetch_ticker_info, ticker))
    news_data = await _in_ctx(loop, partial(storage.fetch_news_headlines, ticker, 10))
    # Ingest-only per-ticker fetches (option_contracts, max_pain, net_prem_ticks,
    # net_flow_expiry, seasonality_ticker) were dropped: ~5 calls × 15 tickers =
    # ~75 UW calls/cycle that no tile reads — a major contributor to the
    # 2026-05-29 429 budget blowout. Re-add when the consuming UI ships (Tile 6
    # contract picker, etc.); the wrappers in storage.py/uw.py remain available.
    # OHLC for Tile 1's price line. 5m candles match the 4hr session view.
    ohlc_data = await _in_ctx(loop, partial(storage.fetch_ohlc, ticker, "5m", is_hot))

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
    flip_pct, wall_up_pct, wall_dn_pct, gex_sign, agg_b, gex_status = _extract_gex(spot_data, spot)
    sector = _extract_sector(info_data, flow_info)
    news_items = _extract_news_items(news_data)
    flow_alerts_detail = _project_flow_alerts(flow_info.get("raw_alerts", []))
    ohlc_bars = _extract_ohlc(ohlc_data)
    ask_side_pct = float(flow_info.get("ask_side_pct", 0.0))
    # Direction first: OPENING flow leads (Ge-Lin-Pearson — opening bets predict,
    # closing bets don't), falling back to total flow then the legacy gamma rule.
    # derive_direction returns the basis so the UI can flag the weaker cases.
    # Tile 2 needs it: it confirms the flow that set THIS direction (flow-side).
    direction, direction_basis = gates.derive_direction(flow_alerts_detail, gex_sign, flip_pct)
    # Tile 2: positioning reality check. 5-session OI from our own parquet
    # archive (tier-independent); per-strike delta from spot_exposures. Confirms
    # OI growth at the flow's strikes on the DIRECTION's side (not the aggregate).
    oi_history = await _in_ctx(loop, partial(storage.read_oi_history, ticker, 5))
    tile2 = _build_tile2(flow_alerts_detail, oi_history, spot_data, spot, direction)
    # Tile 3: real per-strike net dealer gamma for the structural ladder, from
    # the spot-exposures payload already fetched above (no extra UW call).
    tile3 = _build_tile3(spot_data, spot)
    # Chained sector-tide fetch once we know the sector slug. Skip if unknown.
    sector_tide_value = 0.0
    if sector:
        tide_data = await _in_ctx(loop, partial(storage.fetch_sector_tide, sector))
        sector_tide_value = _extract_sector_tide_value(tide_data)

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
    g = gates.compute_gates(raw_row, history=None, event_within_hold=event_within_hold)
    gm = gates.compute_gate_method(raw_row, history=None)
    # When the structural read isn't trustworthy (no γ flip in range, or no
    # greek-exposure data), force the structural gate neutral — don't let a
    # fabricated flip/wall drive a green/red verdict.
    if gex_status != "ok":
        g = {**g, "structural": "yellow"}

    return Row(
        ticker=ticker,
        spot=spot,
        direction=direction,
        direction_basis=direction_basis,
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
        gex_status=gex_status,
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
        tile3=tile3,
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


_HOLD_WINDOW_DAYS = 45   # "near-dated" for Tile 2 confirmation: weeklies + front
                         # monthly. Far-dated OI (LEAPS/quarterlies) rarely reflects
                         # the opening flow we're confirming. Fallback if none match.


def _build_tile2(flow_alerts: list[FlowAlert], oi_history: list[dict],
                 spot_data: Any, spot: float, direction: str = "calls") -> Tile2:
    """Assemble Tile 2's positioning-reality model from:
      - flow_alerts: opening read (volume_oi_ratio) + per-strike premium + expiry
      - oi_history: oldest→newest daily OI snapshots from our parquet archive
      - spot_data: per-strike net delta (greek-exposure)
      - direction: the headline side this tile must CONFIRM ("calls"/"puts")

    Tile 2 confirms the specific opening flow that set the direction: did OI grow
    at the strikes that flow concentrated in, on THAT side, near-dated? It is
    anchored to the flow's side — never the name-wide call+put aggregate, which is
    contaminated (covered calls, spread legs, protective puts) and severs the link
    to the bet we're following.
    """
    # Side we're confirming. Opening read, per-strike premium, OI trend and the
    # confirmation verdict are all measured on THIS side only.
    flow_side = "put" if direction == "puts" else "call"

    # ── Opening read (flow side only) ─────────────────────────────────────
    # Opening intensity = volume_oi_ratio>1 (today's volume exceeded prior OI =
    # net-new positioning). Measured on the flow side so contaminating other-side
    # flow can't move it. (UW's all_opening_trades flag is ~always False on Basic.)
    side_alerts = [a for a in flow_alerts if a.type == flow_side]
    sa_n = len(side_alerts)
    opening_n = sum(1 for a in side_alerts if a.volume_oi_ratio > 1)
    opening_pct = (opening_n / sa_n * 100) if sa_n else 0.0
    # Median, not mean — a single brand-new far-OTM strike (tiny prior OI) can
    # blow a mean vol/OI ratio into the hundreds and misrepresent the batch.
    vois = [a.volume_oi_ratio for a in side_alerts if a.volume_oi_ratio > 0]
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

    # ── Per-(strike, side) OI history (from parquet archive) ──────────────
    # Tile 2 shows ONE (strike, side) at a time — the one with the most flow $ for
    # the toggled side. Aggregate flow premium by (strike, side) and track the top
    # expiry by $ for each, so the toggle can pick "the strike+expiry with the most
    # call (or put) money" and show that strike's side-specific 5-day OI.
    delta_by_strike = _per_strike_net_delta(spot_data)
    prem_ks: dict[tuple, float] = {}            # (strike, side) -> premium
    exp_ks: dict[tuple, dict] = {}              # (strike, side) -> {expiry: premium}
    voi_ks: dict[tuple, list] = {}              # (strike, side) -> [voi]
    for a in flow_alerts:
        if a.strike > 0 and a.type in ("call", "put"):
            key = (a.strike, a.type)
            prem_ks[key] = prem_ks.get(key, 0.0) + a.total_premium
            if a.expiry:
                ed = exp_ks.setdefault(key, {})
                ed[a.expiry] = ed.get(a.expiry, 0.0) + a.total_premium
            if a.volume_oi_ratio > 0:
                voi_ks.setdefault(key, []).append(a.volume_oi_ratio)

    # Focus: top (strike, side) pairs by side premium. Fall back to top-OI strikes
    # (both sides) when flow carries no strike premium.
    if prem_ks:
        focus_ks = sorted(prem_ks, key=lambda key: -prem_ks[key])[:8]
    elif oi_history:
        latest = oi_history[-1]["strikes"]
        focus_ks = [(k, side) for k in sorted(latest, key=lambda x: -latest[x])[:4]
                    for side in ("call", "put")]
    else:
        focus_ks = []

    # Spec: today's OI is NOT settled until ~9am next session, so it is shown
    # as a live vol/OI ratio — never as a settled bar. Treat the most-recent
    # archived session as "today" (provisional) and bar only the settled days.
    today_date = datetime.now(tz=timezone.utc).date().isoformat()
    settled = [s for s in oi_history if s["date"] != today_date]
    settled_dates = [s["date"] for s in settled]

    strike_hist: list[StrikeOIHistory] = []
    for (k, side) in focus_ks:
        bars = [OISessionBar(date=s["date"], oi=int((s.get(side) or {}).get(k, 0)),
                             provisional=False)
                for s in settled]
        delta_oi = 0
        trend = "flat"
        if len(bars) >= 2 and bars[-2].oi > 0:
            delta_oi = bars[-1].oi - bars[-2].oi
            ratio = delta_oi / bars[-2].oi
            trend = "building" if ratio >= 0.05 else "unwinding" if ratio <= -0.05 else "flat"
        top_exp = max(exp_ks.get((k, side), {}), key=lambda e: exp_ks[(k, side)][e], default="")
        strike_hist.append(StrikeOIHistory(
            strike=k,
            side=side,
            expiry=top_exp,
            sessions=bars,
            delta_oi=delta_oi,
            net_delta=_nearest_delta(delta_by_strike, k),
            premium_usd=prem_ks.get((k, side), 0.0),
            trend=trend,
            today_vol_oi=round(_median(voi_ks.get((k, side), [])), 2),
        ))
    # Highest-$ first so the frontend defaults to the top (strike, side).
    strike_hist.sort(key=lambda s: -s.premium_usd)

    # ── Confirmation: flow-side strike CLUSTER, near-dated, day-over-day ───
    # The reality-check question is narrow — "did OI grow at the strikes this
    # directional flow concentrated in, on THIS side, recently?" So confirm on
    # the flow-side cluster only (top-premium flow-side strikes), preferring the
    # near-dated/hold-window expiries, comparing the newest settled session to the
    # prior one. NOT the call+put aggregate (contaminated) and NOT a single strike
    # (idiosyncratic). Building = the directional flow opened; shrinking = it was
    # closing (the signal was noise). This is probabilistic — a call-strike OI
    # build can still be a covered call or spread leg — so it corroborates, not proves.
    today = _date.today()

    def _dte(e: str) -> int:
        try:
            return (_date.fromisoformat(e) - today).days
        except (TypeError, ValueError):
            return -9999

    n_settled = len(settled)
    cluster = [s for s in strike_hist if s.side == flow_side and len(s.sessions) >= 2]
    near = [s for s in cluster if 0 <= _dte(s.expiry) <= _HOLD_WINDOW_DAYS]
    cluster = near or cluster   # fall back to all flow-side strikes if none near-dated

    oi_trend_5d_pct = 0.0
    if n_settled >= 2 and cluster:
        base = sum(s.sessions[-2].oi for s in cluster)
        delta = sum(s.delta_oi for s in cluster)   # newest settled − prior settled
        if base > 0:
            oi_trend_5d_pct = round(delta / base * 100, 1)

    # ── Confirmation state ────────────────────────────────────────────────
    if n_settled < 2 or not cluster:
        confirmation = "unconfirmed"   # not enough settled history / no flow-side strikes
    elif oi_trend_5d_pct >= 5:
        confirmation = "building"
    elif oi_trend_5d_pct <= -5:
        confirmation = "unwinding"
    else:
        confirmation = "flat"

    # ── Low-conviction state (name-wide scatter warning) ──────────────────
    n_expiries = len(prem_by_expiry)
    n_strikes = len(prem_by_strike)
    low_conviction = len(flow_alerts) > 0 and (n_expiries >= 4 or n_strikes >= 8) and \
        (max(prem_by_expiry.values(), default=0) / total_prem if total_prem else 0) < 0.4
    low_msg = (f"Flow scattered across {n_expiries} expirations / {n_strikes} strikes "
               f"— no clear target.") if low_conviction else ""

    return Tile2(
        # Observed side this read confirmed — only when flow actually exists (else
        # the direction was a gamma guess, so there's no observed flow side).
        flow_side=(flow_side if flow_alerts else ""),
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


def _build_tile3(spot_data: Any, spot: float, window_pct: float = 8.0,
                 max_strikes: int = 25) -> Tile3:
    """Real per-strike net dealer gamma for Tile 3's ladder, from the
    greek-exposure (spot-exposures/strike) payload we already fetch. Keeps strikes
    within ±window_pct of spot, nearest-spot first, capped to max_strikes, then
    sorted ascending for rendering. Empty on failure / no spot."""
    if isinstance(spot_data, storage.UWFailure) or spot <= 0:
        return Tile3()
    recs = uw.gex_records(spot_data)  # [{strike, gamma=call_gamma_oi - put_gamma_oi}]
    if not recs:
        return Tile3()
    lo, hi = spot * (1 - window_pct / 100), spot * (1 + window_pct / 100)
    near = [r for r in recs if lo <= r["strike"] <= hi]
    near.sort(key=lambda r: abs(r["strike"] - spot))
    near = near[:max_strikes]
    near.sort(key=lambda r: r["strike"])
    return Tile3(strikes=[Tile3Strike(strike=r["strike"], net_gamma=r["gamma"])
                          for r in near])


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
        # Keep extended-hours candles too (market_time r/pr/po) — the operator
        # wants pre- and post-market action in Tile 1. The chart spans the whole
        # extended trading day (pre-market through after-hours) of the newest bar.
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
    # Keep enough bars for a full EXTENDED trading day (pre 4:00 → post 20:00 ET,
    # 16h / 5m = 192 candles). The chart filters to the newest bar's extended day,
    # so this guarantees the whole day is available even when fetched mid-session.
    bars.sort(key=lambda b: b.t)
    return bars[-200:]


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


def _extract_gex(spot_data: Any, spot: float) -> tuple[float, float, float, str, float, str]:
    """Use v1 gex_records parser to extract per-strike net dealer gamma.
    Returns (flip_dist_pct, wall_up_dist_pct, wall_dn_dist_pct, gex_sign,
    agg_gamma_b, status).

    status: "ok"          — a γ flip exists within the observed strikes;
            "no_flip"     — data present but cumulative γ never crosses zero in
                            range (flip is beyond ±window); flip_dist is 0 and
                            consumers should not draw a flip level;
            "unavailable" — no greek-exposure data for this ticker.

    Flip = the cumulative-γ zero-crossing NEAREST spot (not the first scanning
    from the bottom, which caught noise crossings and, on no crossing, defaulted
    to the lowest strike — the band-edge garbage). Wall = max call-γ strike above
    / max put-γ strike below spot."""
    if isinstance(spot_data, storage.UWFailure) or spot <= 0:
        return 0.0, 5.0, 5.0, "POS", 0.0, "unavailable"
    recs = uw.gex_records(spot_data)
    if not recs:
        return 0.0, 5.0, 5.0, "POS", 0.0, "unavailable"

    def _raw(r, key):
        try: return float((r.get("raw") or {}).get(key) or 0)
        except (TypeError, ValueError): return 0.0
    rungs = [{"strike": r["strike"], "net": r["gamma"],
              "call_gamma": _raw(r, "call_gamma_oi"), "put_gamma": _raw(r, "put_gamma_oi")}
             for r in recs]
    lv = gex.compute_levels(rungs, spot)
    return (lv["flip_pct"], lv["call_wall_pct"], lv["put_wall_pct"],
            lv["gex_sign"], lv["agg_b"], lv["flip_status"])


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
