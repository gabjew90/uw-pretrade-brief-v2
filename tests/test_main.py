"""Tests for FastAPI app: routes, hydration, lifespan."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from server import main, snapshot as snapshot_mod


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


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


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
