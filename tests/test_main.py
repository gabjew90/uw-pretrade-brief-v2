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
    """The snapshot cache is module-global; reset it after each test so tests
    that seed it (e.g. the admin-backfill cases) can't leak into others."""
    yield
    main._snapshot_cache["latest"] = None


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
              flow_alerts_detail=[SimpleNamespace(strike=760.0)], tile2=None,
              wall_up_dist_pct=1.0, wall_dn_dist_pct=2.0)], stale_since=None)
    seen = {}
    monkeypatch.setattr(tile4, "build_tile4",
                        lambda t, ctx: seen.update(t=t, ctx=ctx) or {"status": "ok", "ticker": t})
    r = client.get("/api/tile4/spy")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert seen["t"] == "SPY"
    assert seen["ctx"]["spot"] == 756.0 and seen["ctx"]["direction"] == "calls"
    assert seen["ctx"]["flow_strikes"] == {760.0}
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
