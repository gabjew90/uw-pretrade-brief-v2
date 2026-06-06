import datetime as dt
import importlib

from fastapi.testclient import TestClient


def _stub_snapshot(ticker="ZZZ"):
    from server.schema import Snapshot, Row, Regime, Gates, GateMethod
    return Snapshot(
        fetched_at=dt.datetime.now(tz=dt.timezone.utc),
        regime=Regime(),
        rows=[Row(ticker=ticker, spot=1.0, direction="calls",
                  gates=Gates(flow="green", oi="green", structural="green", cost="green"),
                  gate_method=GateMethod(flow="absolute", oi="absolute",
                                         structural="absolute", cost="absolute"))],
    )


def _load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REPLAY", "1")   # skip the loop; build path is what we test
    import server.main as main
    return importlib.reload(main)


def test_snapshot_json_serves_front_door_and_honors_refresh(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    called = {}

    async def _stub(*, force_flow=False):
        called["force_flow"] = force_flow
        return _stub_snapshot()
    monkeypatch.setattr(main.snapshot_mod, "get_or_build_snapshot", _stub)

    with TestClient(main.app) as c:
        body = c.get("/snapshot.json").json()
        assert any(r["ticker"] == "ZZZ" for r in body["rows"]), "route must serve front-door output"
        assert called["force_flow"] is False
        c.get("/snapshot.json?refresh=1")
        assert called["force_flow"] is True, "?refresh=1 must force the flow rebuild"


def test_root_builds_on_request(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)

    async def _stub(*, force_flow=False):
        return _stub_snapshot("ROOTX")
    monkeypatch.setattr(main.snapshot_mod, "get_or_build_snapshot", _stub)

    with TestClient(main.app) as c:
        html = c.get("/").text
        assert "ROOTX" in html, "/ must hydrate from the front-door build"
