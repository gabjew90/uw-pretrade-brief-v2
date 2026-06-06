import datetime as dt

import pytest

from server import snapshot as snap_mod
from server.schema import Snapshot, Row, Regime, Gates, GateMethod


def _row(t="AAA"):
    return Row(ticker=t, spot=1.0, direction="calls",
               gates=Gates(flow="yellow", oi="yellow", structural="yellow", cost="yellow"),
               gate_method=GateMethod(flow="cross_sectional", oi="absolute",
                                      structural="absolute", cost="percentile"),
               is_light=True)


def _snap(age_s, posture="Stand down", regime_age_s=0, ticker="AAA"):
    now = dt.datetime.now(tz=dt.timezone.utc)
    return Snapshot(
        fetched_at=now - dt.timedelta(seconds=age_s),
        regime=Regime(posture=posture,
                      as_of=(now - dt.timedelta(seconds=regime_age_s)).isoformat()),
        rows=[_row(ticker)],
    )


async def _fresh_build():
    return Snapshot(fetched_at=dt.datetime.now(tz=dt.timezone.utc),
                    regime=_current_regime_placeholder(), rows=[_row("FRESH")])


def _current_regime_placeholder():
    return Regime()


@pytest.mark.asyncio
async def test_serves_cached_when_flow_fresh(monkeypatch):
    snap_mod._RAM["latest"] = _snap(age_s=5)

    def _boom():
        raise AssertionError("should not rebuild when flow is fresh")
    monkeypatch.setattr(snap_mod, "build_light_snapshot", _boom)
    out = await snap_mod.get_or_build_snapshot()
    assert out.rows[0].ticker == "AAA"


@pytest.mark.asyncio
async def test_rebuilds_grid_carries_regime_when_flow_stale_regime_fresh(monkeypatch):
    snap_mod._RAM["latest"] = _snap(age_s=120, posture="Favorable", regime_age_s=30)
    monkeypatch.setattr(snap_mod, "build_light_snapshot", _fresh_build)
    regime_calls = []

    async def _rec(loop):
        regime_calls.append(1)
        return (Regime(posture="NEW"), False)
    monkeypatch.setattr(snap_mod, "_build_market_regime", _rec)
    monkeypatch.setattr(snap_mod.storage, "append_snapshot", lambda d: True)

    out = await snap_mod.get_or_build_snapshot()
    assert regime_calls == [], "fresh regime must be carried forward, not recomputed"
    assert out.regime.posture == "Favorable"
    assert out.rows[0].ticker == "FRESH"


@pytest.mark.asyncio
async def test_force_flow_rebuilds_even_when_fresh(monkeypatch):
    snap_mod._RAM["latest"] = _snap(age_s=5, posture="Favorable", regime_age_s=5)
    monkeypatch.setattr(snap_mod, "build_light_snapshot", _fresh_build)
    monkeypatch.setattr(snap_mod.storage, "append_snapshot", lambda d: True)
    out = await snap_mod.get_or_build_snapshot(force_flow=True)
    assert out.rows[0].ticker == "FRESH"
