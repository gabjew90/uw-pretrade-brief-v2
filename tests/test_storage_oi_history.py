import datetime as dt
import importlib


def _fresh_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from server import storage
    return importlib.reload(storage)


def _payload(strike, call_oi, put_oi):
    return {"data": [{"strike": strike, "call_oi": call_oi, "put_oi": put_oi}]}


def test_oi_history_aggregates_one_bar_per_session_across_many_part_files(tmp_path, monkeypatch):
    storage = _fresh_storage(tmp_path, monkeypatch)
    # Day 1: two intraday {} writes — the LATER one (14:00) is the session bar.
    storage.write_response("oi_per_strike", "AAPL", {}, _payload(100, 5, 0), 200, 5,
                           dt.datetime(2026, 6, 3, 13, 0, 0, tzinfo=dt.timezone.utc))
    storage.write_response("oi_per_strike", "AAPL", {}, _payload(100, 9, 0), 200, 5,
                           dt.datetime(2026, 6, 3, 14, 0, 0, tzinfo=dt.timezone.utc))
    # Day 1 also has a LATER date-param (backfill-style) write that must NOT win
    # over the {} live snapshot for the session bar.
    storage.write_response("oi_per_strike", "AAPL", {"date": "2026-06-03"}, _payload(100, 999, 0), 200, 5,
                           dt.datetime(2026, 6, 3, 15, 0, 0, tzinfo=dt.timezone.utc))
    # Day 2: single write.
    storage.write_response("oi_per_strike", "AAPL", {}, _payload(100, 7, 0), 200, 5,
                           dt.datetime(2026, 6, 4, 14, 0, 0, tzinfo=dt.timezone.utc))

    out = storage.read_oi_history("AAPL", 5)
    # Oldest -> newest, one bar per session.
    assert [s["date"] for s in out] == ["2026-06-03", "2026-06-04"]
    # Day 1 picks the latest {} write (9), NOT the earlier {} (5) and NOT the date-param 999.
    assert out[0]["strikes"][100.0] == 9
    assert out[1]["strikes"][100.0] == 7
