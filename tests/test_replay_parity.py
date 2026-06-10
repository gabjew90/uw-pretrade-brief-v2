"""REPLAY parity gate (ops-ci spec §4): the FULL pipeline run offline from captured
bronze — governor denying every live call, clock frozen — must be deterministic AND match
the committed golden ViewModel. CI is where unintentional drift gets caught; intentional
ViewModel changes re-capture via scripts/capture_golden_vm.py and commit the new snapshot.
"""
import json
from pathlib import Path

import pytest

from server.services import storage
from tests.replay_harness import build_replay_vm, seed_lake

GOLDEN = Path(__file__).parent / "fixtures" / "golden_viewmodel" / "SPY.json"


@pytest.fixture
def lake(tmp_path, monkeypatch):
    roots = {"bronze": tmp_path / "bronze", "silver": tmp_path / "silver",
             "gold": tmp_path / "gold"}
    monkeypatch.setattr(storage, "_tier_root", lambda tier: roots[tier])
    seed_lake(roots)
    return roots


def test_replay_makes_no_live_calls(lake, monkeypatch):
    """In REPLAY no HTTP request may leave the process — ingest serves bronze."""
    import requests

    def _no_network(*a, **k):
        raise AssertionError("REPLAY made a live HTTP call")
    monkeypatch.setattr(requests, "get", _no_network)
    vm = build_replay_vm()
    assert vm.verdict.direction == "puts"          # the golden read, served from bronze


def test_replay_is_deterministic(lake):
    a = build_replay_vm().model_dump(mode="json")
    b = build_replay_vm().model_dump(mode="json")
    assert a == b


def test_replay_matches_committed_golden_viewmodel(lake):
    """Offline bronze → ViewModel must deep-equal the committed snapshot. A diff here is
    either an unintended drift (fix the code) or an intended change (re-run
    scripts/capture_golden_vm.py and commit)."""
    vm = build_replay_vm().model_dump(mode="json")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert vm == golden
