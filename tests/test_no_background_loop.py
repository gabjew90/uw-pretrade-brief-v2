import importlib


def test_no_background_loop_symbols(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REPLAY", "1")
    import server.main as main
    importlib.reload(main)
    # The background loop and its loop-only scaffolding are gone (request-driven).
    assert not hasattr(main, "_refresh_loop"), "background loop must be removed"
    assert not hasattr(main, "_REFRESH_INTERVAL_SECONDS")
    assert not hasattr(main, "_CLOSED_RECHECK_SECONDS")
    assert not hasattr(main, "_next_cached_snapshot")
