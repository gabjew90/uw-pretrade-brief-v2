import importlib


def _fresh_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from server import storage
    return importlib.reload(storage)


def test_read_last_snapshot_returns_latest_good_via_tail(tmp_path, monkeypatch):
    storage = _fresh_storage(tmp_path, monkeypatch)
    storage.append_snapshot({"fetched_at": "2026-06-05T10:00:00Z", "rows": [{"ticker": "AAA"}]})
    storage.append_snapshot({"fetched_at": "2026-06-05T10:02:00Z", "rows": []})            # empty: skipped
    storage.append_snapshot({"fetched_at": "2026-06-05T10:04:00Z", "rows": [{"ticker": "BBB"}]})
    out = storage.read_last_snapshot()
    assert out is not None and out["rows"][0]["ticker"] == "BBB"


def test_append_snapshot_caps_file_lines(tmp_path, monkeypatch):
    storage = _fresh_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "_MAX_SNAPSHOT_LINES", 10, raising=False)
    for i in range(25):
        storage.append_snapshot({"fetched_at": f"2026-06-05T10:{i:02d}:00Z", "rows": [{"ticker": f"T{i}"}]})
    lines = [ln for ln in storage._snapshots_path().read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) <= 10, "log must be capped"
    out = storage.read_last_snapshot()
    assert out["rows"][0]["ticker"] == "T24", "newest survives the cap"
