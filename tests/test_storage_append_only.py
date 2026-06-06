import datetime as dt
import importlib


def _fresh_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from server import storage
    return importlib.reload(storage)


def test_concurrent_writes_same_hour_no_lost_update(tmp_path, monkeypatch):
    storage = _fresh_storage(tmp_path, monkeypatch)
    at = dt.datetime(2026, 6, 5, 14, 0, 0, tzinfo=dt.timezone.utc)
    # Two writes to the SAME endpoint/ticker/hour in the same second.
    assert storage.write_response("oi_per_strike", "AAPL", {}, {"data": [{"strike": 100, "call_oi": 1, "put_oi": 0}]}, 200, 5, at)
    assert storage.write_response("oi_per_strike", "AAPL", {}, {"data": [{"strike": 101, "call_oi": 2, "put_oi": 0}]}, 200, 5, at)
    d = tmp_path / "raw" / "endpoint=oi_per_strike" / "dt=2026-06-05" / "ticker=AAPL"
    parts = list(d.glob("part-*.parquet"))
    assert len(parts) == 2, "both writes must persist as separate part files (no lost update)"
    assert not list(d.glob("*.tmp")), "no temp file left behind (atomic temp->replace)"
