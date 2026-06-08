"""Governor tests (Phase 1) — budget gate, header authority, persistence, coalescing.

Deterministic: time is injected (`now`), each test uses a FRESH Governor (no shared
singleton state) with the persist path redirected to a tmp dir.
"""
import json
import threading
import time
from datetime import datetime, timezone

from server.services.governor import (Governor, Priority, _MINUTE_HEADROOM,
                                       _PERSIST_EVERY)

UTC = timezone.utc
NOW = datetime(2026, 6, 8, 15, 0, tzinfo=UTC)   # a fixed UTC instant


def make_gov(tmp_path, *, cap=100, soft=0.9, replay=False):
    g = Governor()
    g.replay = replay
    g.meter.cap_day = cap
    g.meter.soft_pct = soft
    g.meter.day_key = NOW.date().isoformat()   # pre-seed so _roll() doesn't reset on first NOW
    g._persist_path = lambda: tmp_path / "uw_budget.json"
    return g


# ── budget decisions ─────────────────────────────────────────────────────────
def test_replay_denies_unconditionally(tmp_path):
    g = make_gov(tmp_path, replay=True)
    g.meter.day_count = 0
    assert g.check(priority=Priority.CRITICAL, now=NOW).allow is False


def test_hard_cap_denies_all_priorities(tmp_path):
    g = make_gov(tmp_path, cap=10)
    g.meter.day_count = 10
    assert g.check(priority=Priority.NORMAL, now=NOW).allow is False
    assert g.check(priority=Priority.CRITICAL, now=NOW).allow is False


def test_soft_cap_sheds_noncritical_only(tmp_path):
    g = make_gov(tmp_path, cap=100, soft=0.9)
    g.meter.day_count = 95                              # >= 90 soft, < 100 hard
    assert g.check(priority=Priority.NORMAL, now=NOW).allow is False
    assert g.check(priority=Priority.CRITICAL, now=NOW).allow is True


def test_per_minute_gate_denies_all(tmp_path):
    g = make_gov(tmp_path, cap=10_000)
    g.record(_MINUTE_HEADROOM, now=NOW)                 # 115 calls inside the 60s window
    assert g.check(priority=Priority.CRITICAL, now=NOW).reason == "per-minute limit"
    assert g.check(priority=Priority.CRITICAL, now=NOW).allow is False


def test_minute_window_rolls_off_after_60s(tmp_path):
    g = make_gov(tmp_path, cap=10_000)
    g.record(_MINUTE_HEADROOM, now=NOW)
    later = datetime(2026, 6, 8, 15, 1, 1, tzinfo=UTC)  # 61s later
    assert g.check(priority=Priority.CRITICAL, now=later).allow is True


# ── header authority ─────────────────────────────────────────────────────────
def test_headers_override_local_cap_and_count(tmp_path):
    g = make_gov(tmp_path, cap=15_000)
    g.update_from_headers({"x-uw-token-req-limit": "12000",
                           "x-uw-daily-req-count": "11999"}, now=NOW)
    snap = g.snapshot(now=NOW)
    assert snap["source"] == "uw_headers"
    assert snap["daily_cap"] == 12000                  # header cap (lower) wins over env 15000
    assert snap["calls_today"] == 11999
    # 11999 >= 0.9*12000 (=10800) → NORMAL shed, CRITICAL still under 12000 → allowed
    assert g.check(priority=Priority.NORMAL, now=NOW).allow is False
    assert g.check(priority=Priority.CRITICAL, now=NOW).allow is True


def test_snapshot_source_local_until_headers_seen(tmp_path):
    g = make_gov(tmp_path)
    assert g.snapshot(now=NOW)["source"] == "local"
    g.update_from_headers({"x-uw-daily-req-count": "5"}, now=NOW)
    assert g.snapshot(now=NOW)["source"] == "uw_headers"


# ── persistence ──────────────────────────────────────────────────────────────
def test_load_persisted_restores_today(tmp_path):
    (tmp_path / "uw_budget.json").write_text(json.dumps({"day": "2026-06-08", "count": 42}))
    g = make_gov(tmp_path)
    g.load_persisted(now=NOW)
    assert g.meter.day_count == 42


def test_load_persisted_ignores_previous_day(tmp_path):
    (tmp_path / "uw_budget.json").write_text(json.dumps({"day": "2020-01-01", "count": 99}))
    g = make_gov(tmp_path)
    g.load_persisted(now=NOW)
    assert g.meter.day_count == 0


def test_flush_is_atomic_no_tmp_left(tmp_path):
    g = make_gov(tmp_path)
    g.record(_PERSIST_EVERY, now=NOW)                  # crosses the flush threshold
    p = tmp_path / "uw_budget.json"
    assert p.exists()
    assert json.loads(p.read_text())["count"] == _PERSIST_EVERY
    assert not list(tmp_path.glob("*.tmp"))            # temp file replaced, none leaked


def test_reset_clears_memory_not_file(tmp_path):
    p = tmp_path / "uw_budget.json"
    g = make_gov(tmp_path)
    g.record(_PERSIST_EVERY, now=NOW)                  # writes the file + bumps counters
    assert p.exists()
    g.reset()
    assert g.meter.day_count == 0
    assert g.meter.per_minute() == 0
    assert p.exists()                                  # persisted file untouched by reset


# ── request coalescing ───────────────────────────────────────────────────────
def test_coalesce_runs_producer_once(tmp_path):
    g = make_gov(tmp_path)
    started, release = threading.Event(), threading.Event()
    waiter_entered = threading.Event()
    leader_calls, waiter_calls, results = [], [], {}

    def leader_producer():
        started.set()
        release.wait(2)
        leader_calls.append(1)
        return "RESULT"

    def waiter_producer():
        waiter_calls.append(1)            # must NOT run — coalesced onto the leader
        return "WAITER"

    def run_leader():
        results["a"] = g.coalesce("k", leader_producer)

    def run_waiter():
        waiter_entered.set()
        results["b"] = g.coalesce("k", waiter_producer)

    ta = threading.Thread(target=run_leader); ta.start()
    assert started.wait(2)                 # leader holds the in-flight key, blocked on release
    tb = threading.Thread(target=run_waiter); tb.start()
    assert waiter_entered.wait(2)
    time.sleep(0.2)                        # let the waiter park in event.wait() (key still held)
    release.set()
    ta.join(3); tb.join(3)

    assert results["a"] == "RESULT"
    assert results["b"] == "RESULT"        # waiter shared the leader's result
    assert leader_calls == [1]
    assert waiter_calls == []              # producer ran exactly once


def test_coalesce_shares_exception(tmp_path):
    g = make_gov(tmp_path)
    started, release = threading.Event(), threading.Event()
    waiter_entered = threading.Event()
    errors = {}

    class Boom(Exception):
        pass

    def leader_producer():
        started.set()
        release.wait(2)
        raise Boom("leader failed")

    def run_leader():
        try:
            g.coalesce("k", leader_producer)
        except Boom as e:
            errors["a"] = str(e)

    def run_waiter():
        waiter_entered.set()
        try:
            g.coalesce("k", lambda: "should-not-run")
        except Boom as e:
            errors["b"] = str(e)

    ta = threading.Thread(target=run_leader); ta.start()
    assert started.wait(2)
    tb = threading.Thread(target=run_waiter); tb.start()
    assert waiter_entered.wait(2)
    time.sleep(0.2)
    release.set()
    ta.join(3); tb.join(3)

    assert errors["a"] == "leader failed"
    assert errors["b"] == "leader failed"   # waiter received the leader's exception
