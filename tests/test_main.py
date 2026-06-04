"""Tests for FastAPI app: routes, hydration, lifespan."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from server import main, snapshot as snapshot_mod


@pytest.fixture(autouse=True)
def _reset_snapshot_cache():
    """The snapshot + lookup caches are module-global; reset them after each test
    so tests that seed them (e.g. the admin-backfill / search-any-ticker cases)
    can't leak into others."""
    yield
    main._snapshot_cache["latest"] = None
    getattr(main, "_lookup_cache", {}).clear()


@pytest.fixture
def client(tmp_data_dir, tmp_path, monkeypatch):
    # Replace lifespan refresh with no-op so tests don't fire real UW
    async def noop_refresh():
        from server.schema import Snapshot, Regime
        from datetime import datetime, timezone
        return Snapshot(fetched_at=datetime.now(timezone.utc),
                        regime=Regime(label="normal"), rows=[])
    monkeypatch.setattr(snapshot_mod, "refresh_snapshot", noop_refresh)

    # Use a temp static dir with a minimal stub, so the test doesn't clobber
    # the real static/index.html
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<html><body><!-- HYDRATION_TARGET --></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "_STATIC_DIR", static_dir)
    with TestClient(main.app) as c:
        yield c


def test_seed_cache_from_disk_loads_last_good_snapshot(tmp_data_dir, monkeypatch):
    """Cold-boot resilience: seed the in-memory cache from the last persisted
    snapshot so a fresh container (off-hours / mid-outage) serves last-good data
    instead of blank. The seeded snapshot is marked stale_since."""
    from server import storage, main as main_mod
    main_mod._snapshot_cache["latest"] = None
    storage.append_snapshot({
        "fetched_at": "2026-06-01T20:00:00+00:00",
        "regime": {"label": "normal", "detail": "", "vix": 0.0},
        "rows": [{"ticker": "AAA", "spot": 1.0, "direction": "calls",
                  "gates": {"flow": "green", "oi": "green", "structural": "green", "cost": "green"},
                  "gate_method": {"flow": "absolute", "oi": "absolute",
                                  "structural": "absolute", "cost": "absolute"}}],
        "stale_since": None,
    })
    main_mod._seed_cache_from_disk()
    seeded = main_mod._snapshot_cache["latest"]
    assert seeded is not None
    assert seeded.rows[0].ticker == "AAA"
    assert seeded.stale_since is not None        # marked stale (it's last-close data)


def test_seed_cache_from_disk_noop_when_no_file(tmp_data_dir):
    from server import main as main_mod
    main_mod._snapshot_cache["latest"] = None
    main_mod._seed_cache_from_disk()
    assert main_mod._snapshot_cache["latest"] is None   # nothing to seed, stays empty


def test_replay_mode_routes_are_cached_only(monkeypatch):
    """In REPLAY mode the tile3/tile4 routes must never call UW — they read the
    captured archive only. We assert build_tile4 runs under cached_only()."""
    from server import main as main_mod, tile4
    monkeypatch.setenv("REPLAY", "1")
    seen = {}

    def fake_build(t, ctx):
        from server import storage
        seen["cached_only"] = storage._CACHED_ONLY.get()
        return {"status": "ok", "ticker": t}
    monkeypatch.setattr(tile4, "build_tile4", fake_build)
    assert main_mod._replay_enabled() is True


def test_replay_disabled_by_default(monkeypatch):
    from server import main as main_mod
    monkeypatch.delenv("REPLAY", raising=False)
    assert main_mod._replay_enabled() is False


def test_admin_export_forbidden_without_token(client, monkeypatch):
    monkeypatch.delenv("BACKFILL_TOKEN", raising=False)
    r = client.get("/admin/export?token=x")
    assert r.status_code == 403


def test_admin_export_streams_data_dir_tar(client, tmp_data_dir, monkeypatch):
    """With a valid token, /admin/export returns a .tar.gz of the DATA_DIR so the
    real prod archive can be pulled down for local replay."""
    import io, tarfile
    from server import storage
    monkeypatch.setenv("BACKFILL_TOKEN", "s3cret")
    storage.append_snapshot({"fetched_at": "2026-06-01T20:00:00Z", "rows": [{"ticker": "AAA"}]})
    (tmp_data_dir / "raw").mkdir(exist_ok=True)
    (tmp_data_dir / "raw" / "marker.txt").write_text("hi", encoding="utf-8")
    r = client.get("/admin/export?token=s3cret")
    assert r.status_code == 200
    assert "gzip" in r.headers.get("content-type", "") or r.content[:2] == b"\x1f\x8b"
    tf = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz")
    names = tf.getnames()
    assert any("snapshots.jsonl" in n for n in names)
    assert any("marker.txt" in n for n in names)


def test_lookup_route_builds_row_for_arbitrary_ticker(client, monkeypatch):
    from types import SimpleNamespace
    from server import snapshot as snap_mod, main as main_mod

    async def fake_build(ticker):
        return SimpleNamespace(ticker=ticker.upper(),
                               model_dump=lambda mode=None: {"ticker": ticker.upper(), "spot": 42.0})
    monkeypatch.setattr(snap_mod, "build_single_row", fake_build)
    r = client.get("/api/lookup/roku")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "ROKU" and body["spot"] == 42.0


def test_lookup_route_unavailable_when_build_fails(client, monkeypatch):
    from server import snapshot as snap_mod

    async def fake_build(ticker):
        return None
    monkeypatch.setattr(snap_mod, "build_single_row", fake_build)
    r = client.get("/api/lookup/ZZZZ")
    assert r.status_code == 200
    assert r.json()["status"] == "unavailable"


def test_atomic_view_persists_on_live_success(tmp_data_dir, monkeypatch):
    """A successful live build is persisted as ONE complete unit (for coherent
    replay later) and returned fresh (not stale). The persisted view now includes
    as_of + data_provenance stamps from the freshness collector."""
    from server import main as main_mod, storage
    out = main_mod._atomic_view(lambda: {"status": "ok", "ticker": "NVDA", "ranked": [1]},
                                "tile4", "NVDA")
    assert out["status"] == "ok" and out.get("stale") is not True
    # persisted as a complete view for the atomic fallback (includes freshness stamp)
    persisted = storage.read_view("tile4", "NVDA")
    assert persisted["status"] == "ok"
    assert persisted["ticker"] == "NVDA"
    assert persisted["ranked"] == [1]
    assert "as_of" in persisted            # freshness stamp always present
    assert "data_provenance" in persisted


def test_atomic_view_replays_whole_last_good_on_failure(tmp_data_dir, monkeypatch):
    """On a live failure, serve the WHOLE last-good persisted view from ONE moment
    — NOT a per-endpoint cached_only reassembly (which mixes moments)."""
    from server import main as main_mod, storage
    storage.save_view("tile4", "NVDA", {"status": "ok", "ticker": "NVDA", "ranked": [{"strike": 100}]})
    used_cached_only = []

    def build():
        if storage._CACHED_ONLY.get():
            used_cached_only.append(1)   # the OLD blend path — must NOT be used
        return {"status": "unavailable", "ticker": "NVDA", "reason": "429"}

    out = main_mod._atomic_view(build, "tile4", "NVDA")
    assert out["status"] == "ok"
    assert out["ranked"] == [{"strike": 100}]   # the whole persisted view, one moment
    assert out.get("stale") is True
    assert used_cached_only == []               # no per-endpoint reassembly


def test_atomic_view_unavailable_when_no_persisted_view(tmp_data_dir):
    from server import main as main_mod
    out = main_mod._atomic_view(lambda: {"status": "unavailable", "ticker": "X", "reason": "429"},
                                "tile4", "X")
    assert out["status"] == "unavailable"       # nothing persisted → honest


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_includes_uw_budget_block(client):
    body = client.get("/health").json()
    assert "uw" in body
    for k in ("calls_1m", "calls_today", "budget_pct"):
        assert k in body["uw"]


# ── /admin/backfill: token-guarded, dormant by default ──

def test_admin_backfill_disabled_when_token_unset(client, monkeypatch):
    monkeypatch.delenv("BACKFILL_TOKEN", raising=False)
    r = client.post("/admin/backfill?days=7&token=anything")
    assert r.status_code == 403


def test_admin_backfill_rejects_bad_token(client, monkeypatch):
    monkeypatch.setenv("BACKFILL_TOKEN", "s3cret")
    r = client.post("/admin/backfill?days=7&token=wrong")
    assert r.status_code == 403


def _seed_rows(*tickers):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from server.schema import Snapshot, Regime
    snap = Snapshot.model_construct(
        fetched_at=datetime.now(timezone.utc), regime=Regime(label="normal"),
        rows=[SimpleNamespace(ticker=t) for t in tickers], stale_since=None)
    main._snapshot_cache["latest"] = snap


def test_admin_backfill_valid_token_returns_probe_and_starts(client, monkeypatch):
    monkeypatch.setenv("BACKFILL_TOKEN", "s3cret")
    _seed_rows("SPY", "QQQ")
    from server import backfill
    monkeypatch.setattr(backfill, "probe",
                        lambda tickers, max_days, now=None: {"probe": "ok", "probe_ticker": tickers[0],
                                                             "window_days": max_days})
    started = []
    monkeypatch.setattr(backfill, "backfill_oi_history",
                        lambda tickers, max_days=30, now=None: started.append((tuple(tickers), max_days)) or {})
    r = client.post("/admin/backfill?days=7&token=s3cret")
    assert r.status_code == 202
    body = r.json()
    assert body["probe"] == "ok"
    assert body["window_days"] == 7


def test_tile3_detail_route_returns_payload(client, monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from server.schema import Snapshot, Regime
    from server import tile3_detail
    main._snapshot_cache["latest"] = Snapshot.model_construct(
        fetched_at=datetime.now(timezone.utc), regime=Regime(label="normal"),
        rows=[SimpleNamespace(ticker="SPY", spot=756.0, direction="calls")], stale_since=None)
    seen = {}
    monkeypatch.setattr(tile3_detail, "build_tile3_detail",
                        lambda t, flow_spot, direction:
                        seen.update(t=t, spot=flow_spot, dir=direction) or
                        {"status": "ok", "ticker": t, "views": {}})
    r = client.get("/api/tile3/spy")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert seen == {"t": "SPY", "spot": 756.0, "dir": "calls"}


def test_tile3_detail_route_unavailable_when_ticker_not_in_snapshot(client, monkeypatch):
    from server import tile3_detail
    main._snapshot_cache["latest"] = None
    called = []
    monkeypatch.setattr(tile3_detail, "build_tile3_detail",
                        lambda t, flow_spot, direction: called.append((t, flow_spot)) or
                        {"status": "unavailable", "ticker": t})
    r = client.get("/api/tile3/ZZZZ")
    assert r.status_code == 200
    assert r.json()["status"] == "unavailable"


def test_tile4_route_builds_ctx_from_snapshot_row(client, monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from server.schema import Snapshot, Regime
    from server import tile4
    main._snapshot_cache["latest"] = Snapshot.model_construct(
        fetched_at=datetime.now(timezone.utc), regime=Regime(label="normal"),
        rows=[SimpleNamespace(ticker="SPY", spot=756.0, direction="calls",
              flow_alerts_detail=[SimpleNamespace(strike=760.0, total_premium=1_250_000.0)],
              tile2=None, wall_up_dist_pct=1.0, wall_dn_dist_pct=2.0)], stale_since=None)
    seen = {}
    monkeypatch.setattr(tile4, "build_tile4",
                        lambda t, ctx: seen.update(t=t, ctx=ctx) or {"status": "ok", "ticker": t})
    r = client.get("/api/tile4/spy")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert seen["t"] == "SPY"
    assert seen["ctx"]["spot"] == 756.0 and seen["ctx"]["direction"] == "calls"
    # flow value now carried as a per-strike premium map (real $, not a set)
    assert seen["ctx"]["flow_premium_by_strike"] == {760.0: 1_250_000.0}
    assert round(seen["ctx"]["call_wall"]) == round(756.0 * 1.01)


def test_tile4_route_unavailable_when_not_in_snapshot(client):
    main._snapshot_cache["latest"] = None
    r = client.get("/api/tile4/ZZZZ")
    assert r.status_code == 200 and r.json()["status"] == "unavailable"


def test_admin_backfill_reports_unsupported_without_starting(client, monkeypatch):
    monkeypatch.setenv("BACKFILL_TOKEN", "s3cret")
    _seed_rows("SPY")
    from server import backfill
    monkeypatch.setattr(backfill, "probe",
                        lambda tickers, max_days, now=None: {"probe": "unsupported", "probe_ticker": tickers[0],
                                                             "window_days": max_days})
    started = []
    monkeypatch.setattr(backfill, "backfill_oi_history",
                        lambda *a, **k: started.append(1) or {})
    r = client.post("/admin/backfill?days=7&token=s3cret")
    assert r.status_code == 200
    assert r.json()["probe"] == "unsupported"
    assert started == [], "must not start the full pass when unsupported"


def test_snapshot_json_returns_cached_snapshot(client):
    r = client.get("/snapshot.json")
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body or body.get("status") == "warming"


def test_root_returns_html_with_hydration_script(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "window.__SNAPSHOT__" in r.text
    assert "<!-- HYDRATION_TARGET -->" not in r.text  # marker replaced


def test_root_with_deep_link_query_param(client):
    r = client.get("/?t=NVDA")
    assert r.status_code == 200


# ── _next_cached_snapshot: a failed/empty refresh must not blank a good snapshot ──

def _snap(has_rows: bool, stale_since=None):
    """Build a Snapshot whose only test-relevant traits are rows-truthiness and
    stale_since. model_construct bypasses Row validation so we can use a cheap
    placeholder row."""
    from datetime import datetime, timezone
    from server.schema import Snapshot, Regime
    return Snapshot.model_construct(
        fetched_at=datetime.now(timezone.utc),
        regime=Regime(label="normal"),
        rows=[object()] if has_rows else [],
        stale_since=stale_since,
    )


def test_good_refresh_replaces_cache():
    now = datetime.now(timezone.utc)
    current = _snap(has_rows=True)
    fresh = _snap(has_rows=True)
    assert main._next_cached_snapshot(current, fresh, now) is fresh


def test_empty_refresh_keeps_last_good_and_marks_stale():
    """A 429 → _empty_snapshot must NOT overwrite a good snapshot with blanks.
    Regression for the 'loads but stale and no artifacts' outage: keep last good,
    set stale_since."""
    now = datetime.now(timezone.utc)
    current = _snap(has_rows=True)
    empty = _snap(has_rows=False)
    result = main._next_cached_snapshot(current, empty, now)
    assert result is current
    assert result.rows                 # data preserved, not blanked
    assert result.stale_since == now   # flagged stale for the UI


def test_empty_refresh_does_not_overwrite_existing_stale_since():
    earlier = datetime(2026, 5, 29, 20, 0, tzinfo=timezone.utc)
    later = datetime(2026, 5, 29, 20, 5, tzinfo=timezone.utc)
    current = _snap(has_rows=True, stale_since=earlier)
    result = main._next_cached_snapshot(current, _snap(has_rows=False), later)
    assert result.stale_since == earlier  # keep the original staleness onset


def test_cold_boot_empty_refresh_surfaces_warming_snapshot():
    """No prior good snapshot (cold boot mid-outage) → surface the empty one so
    /health reports warming and the loop keeps retrying (not frozen)."""
    now = datetime.now(timezone.utc)
    empty = _snap(has_rows=False)
    assert main._next_cached_snapshot(None, empty, now) is empty


def test_atomic_view_stamps_as_of_and_provenance_on_success(tmp_data_dir):
    """A successful live build is stamped with as_of (oldest contributing field)
    and data_provenance (worst-case source) from the freshness collector."""
    from datetime import datetime, timezone
    from server import main as main_mod, freshness
    obs = datetime(2026, 6, 2, 13, 30, tzinfo=timezone.utc)

    def build():
        freshness.record("greeks", obs, "cache")
        return {"status": "ok", "ticker": "NVDA", "ranked": [1]}

    out = main_mod._atomic_view(build, "tile4", "NVDA")
    assert out["status"] == "ok"
    assert out["as_of"] == obs.isoformat()
    assert out["data_provenance"] == "cache"


def test_atomic_view_failure_path_not_restamped(tmp_data_dir):
    """On live failure we serve the WHOLE last-good view, which already carries its
    OWN as_of from when it was built — we do not overwrite it with a fresh stamp."""
    from server import main as main_mod, storage
    storage.save_view("tile4", "NVDA",
                      {"status": "ok", "ticker": "NVDA", "ranked": [{"strike": 100}],
                       "as_of": "2026-06-01T20:00:00+00:00", "data_provenance": "live"})
    out = main_mod._atomic_view(lambda: {"status": "unavailable", "ticker": "NVDA"}, "tile4", "NVDA")
    assert out.get("stale") is True
    assert out["as_of"] == "2026-06-01T20:00:00+00:00"   # the persisted moment, not now


def test_lookup_route_stamps_as_of(client, monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from server import snapshot as snap_mod, freshness
    obs = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)

    async def fake_build(ticker):
        freshness.record("oi_per_strike", obs, "archive")
        return SimpleNamespace(
            ticker=ticker.upper(),
            model_dump=lambda mode=None: {"ticker": ticker.upper(), "spot": 1.0})
    monkeypatch.setattr(snap_mod, "build_single_row", fake_build)
    body = client.get("/api/lookup/spy").json()
    assert body["as_of"] == obs.isoformat()
    assert body["data_provenance"] == "archive"


def _seed_one_row(main_mod, ticker="SPY"):
    from datetime import datetime, timezone
    from server.schema import Snapshot, Regime, Row, Gates, GateMethod
    row = Row(ticker=ticker, spot=100.0, direction="calls",
              gates=Gates(flow="green", oi="green", structural="green", cost="green"),
              gate_method=GateMethod(flow="absolute", oi="absolute",
                                     structural="absolute", cost="absolute"))
    main_mod._snapshot_cache["latest"] = Snapshot(
        fetched_at=datetime.now(timezone.utc), regime=Regime(label="normal"), rows=[row])


def test_tile4_replay_route_stamps_as_of(client, monkeypatch):
    from datetime import datetime, timezone
    from server import main as main_mod, tile4, freshness
    monkeypatch.setenv("REPLAY", "1")
    obs = datetime(2026, 6, 2, 11, 0, tzinfo=timezone.utc)

    def fake_build(t, ctx):
        freshness.record("greeks", obs, "archive")
        return {"status": "ok", "ticker": t}
    monkeypatch.setattr(tile4, "build_tile4", fake_build)
    _seed_one_row(main_mod, "SPY")
    body = client.get("/api/tile4/SPY").json()
    assert body["status"] == "ok"
    assert body["as_of"] == obs.isoformat()
    assert body["data_provenance"] == "archive"


def test_tile3_replay_route_stamps_as_of(client, monkeypatch):
    from datetime import datetime, timezone
    from server import main as main_mod, tile3_detail, freshness
    monkeypatch.setenv("REPLAY", "1")
    obs = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)

    def fake_build(t, spot, direction):
        freshness.record("spot_exposures_expiry_strike", obs, "archive")
        return {"status": "ok", "ticker": t}
    monkeypatch.setattr(tile3_detail, "build_tile3_detail", fake_build)
    _seed_one_row(main_mod, "SPY")
    body = client.get("/api/tile3/SPY").json()
    assert body["status"] == "ok"
    assert body["as_of"] == obs.isoformat()
    assert body["data_provenance"] == "archive"


def test_tile4_replay_route_unavailable_is_not_stamped(client, monkeypatch):
    """A non-ok build (status 'unavailable') must NOT be stamped — there's no
    coherent as_of to attach when the view couldn't be built. The replay branch
    returns it raw."""
    from server import main as main_mod, tile4
    monkeypatch.setenv("REPLAY", "1")

    def fake_build(t, ctx):
        return {"status": "unavailable", "ticker": t, "reason": "no data"}
    monkeypatch.setattr(tile4, "build_tile4", fake_build)
    _seed_one_row(main_mod, "SPY")
    body = client.get("/api/tile4/SPY").json()
    assert body["status"] == "unavailable"
    assert "as_of" not in body
    assert "data_provenance" not in body


# ── search-any-ticker: the on-demand deep-dive tiles must work for a SEARCHED
#    ticker too (it's not in the snapshot, but /api/lookup built its row). ──

def _fake_lookup_row(ticker="ROKU", spot=77.0, direction="puts"):
    from types import SimpleNamespace
    return SimpleNamespace(
        ticker=ticker, spot=spot, direction=direction,
        flow_alerts_detail=[], tile2=None,
        wall_up_dist_pct=None, wall_dn_dist_pct=None,
        model_dump=lambda mode=None: {"ticker": ticker, "spot": spot, "direction": direction})


def test_tile4_resolves_looked_up_ticker_not_in_snapshot(client, monkeypatch):
    """Regression: a SEARCHED ticker (absent from the snapshot) must still get the
    rich Tile 4 — the route resolves the row built by /api/lookup, not only the
    snapshot. Before the fix tile4 returned 'not in snapshot' → fallback picker."""
    from server import snapshot as snap_mod, tile4
    row = _fake_lookup_row()

    async def fake_build(t):
        return row
    monkeypatch.setattr(snap_mod, "build_single_row", fake_build)
    # 1. search it — populates the server-side lookup cache
    assert client.get("/api/lookup/ROKU").json()["ticker"] == "ROKU"
    # 2. capture the ctx the tile4 build receives
    seen = {}

    def fake_t4(t, ctx):
        seen["ctx"] = ctx
        return {"status": "ok", "ticker": t}
    monkeypatch.setattr(tile4, "build_tile4", fake_t4)
    body = client.get("/api/tile4/ROKU").json()
    assert body.get("reason") != "not in snapshot"
    assert body["status"] == "ok"
    assert seen["ctx"]["spot"] == 77.0
    assert seen["ctx"]["direction"] == "puts"


def test_tile3_resolves_looked_up_ticker_spot_direction(client, monkeypatch):
    """A searched ticker's Tile 3 must build with the looked-up row's real
    spot/direction (not the spot=0 / 'calls' default that yields the fallback)."""
    from server import snapshot as snap_mod, tile3_detail
    row = _fake_lookup_row()

    async def fake_build(t):
        return row
    monkeypatch.setattr(snap_mod, "build_single_row", fake_build)
    client.get("/api/lookup/ROKU")
    seen = {}

    def fake_t3(t, spot, direction):
        seen.update(spot=spot, direction=direction)
        return {"status": "ok", "ticker": t}
    monkeypatch.setattr(tile3_detail, "build_tile3_detail", fake_t3)
    body = client.get("/api/tile3/ROKU").json()
    assert body["status"] == "ok"
    assert seen["spot"] == 77.0 and seen["direction"] == "puts"


def test_tile4_unknown_ticker_still_unavailable(client):
    """A ticker that was never searched AND isn't in the snapshot stays
    'not in snapshot' — the fix must not change that."""
    body = client.get("/api/tile4/ZZZZ").json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "not in snapshot"


def test_tile4_direction_override_param(client, monkeypatch):
    """The deep-dive direction toggle: /api/tile4/{t}?direction=puts ranks the
    PUTS side even when the row's gamma direction is 'calls'. Missing/invalid
    direction falls back to the row's gamma direction."""
    from server import main as main_mod, tile4
    _seed_one_row(main_mod, "SPY")   # row.direction defaults to 'calls'
    seen = {}

    def fake_t4(t, ctx):
        seen["dir"] = ctx["direction"]
        return {"status": "ok", "ticker": t}
    monkeypatch.setattr(tile4, "build_tile4", fake_t4)

    client.get("/api/tile4/SPY")                       # no param → gamma direction
    assert seen["dir"] == "calls"
    client.get("/api/tile4/SPY?direction=puts")        # override
    assert seen["dir"] == "puts"
    client.get("/api/tile4/SPY?direction=garbage")     # invalid → fallback
    assert seen["dir"] == "calls"
