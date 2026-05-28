"""Tests for FastAPI app: routes, hydration, lifespan."""
from __future__ import annotations
import json
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
