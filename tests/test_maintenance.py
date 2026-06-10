"""In-app maintenance scheduler tests — next-run math (ET, DST-implicit via zoneinfo),
run_all marker + isolation (one failing task never stops the rest), and the REPLAY guard.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from server.services import maintenance

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


# ── next_run: the next 03:00 ET strictly after now ────────────────────────────
def test_next_run_before_3am_is_today():
    now = datetime(2026, 6, 10, 1, 30, tzinfo=ET)
    assert maintenance.next_run(now) == datetime(2026, 6, 10, 3, 0, tzinfo=ET)


def test_next_run_after_3am_is_tomorrow():
    now = datetime(2026, 6, 10, 9, 0, tzinfo=ET)
    assert maintenance.next_run(now) == datetime(2026, 6, 11, 3, 0, tzinfo=ET)


def test_next_run_accepts_utc_now():
    now = datetime(2026, 6, 10, 5, 0, tzinfo=UTC)          # 01:00 ET (EDT)
    assert maintenance.next_run(now) == datetime(2026, 6, 10, 3, 0, tzinfo=ET)


# ── run_all: marker written, failures isolated ────────────────────────────────
def test_run_all_writes_marker_and_isolates_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "_marker_path", lambda: tmp_path / "maintenance.json")

    def runner(args):
        if "compact.py" in args[0]:
            raise RuntimeError("boom")                      # one task explodes
        return 0, "ok line"
    marker = maintenance.run_all(runner=runner)
    assert marker["results"]["backup"]["ok"] is True
    assert marker["results"]["compact"]["ok"] is False      # captured, not raised
    assert marker["results"]["backtest"]["ok"] is True      # later task still ran
    on_disk = json.loads((tmp_path / "maintenance.json").read_text())
    assert on_disk["results"] == marker["results"]
    assert maintenance.last_run() == on_disk


def test_start_noop_in_replay(monkeypatch):
    import types
    monkeypatch.setattr(maintenance, "settings", types.SimpleNamespace(replay=True))
    assert maintenance.start() is False


def test_start_noop_when_disabled(monkeypatch):
    import types
    monkeypatch.setattr(maintenance, "settings", types.SimpleNamespace(replay=False))
    monkeypatch.setenv("MAINTENANCE", "0")
    assert maintenance.start() is False
