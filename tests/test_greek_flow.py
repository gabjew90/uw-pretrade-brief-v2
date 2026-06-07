"""Golden-fixture tests for the greek-flow delta composite — the two HARD blockers
from the spec, anchored to a real captured payload (SPY 2026-06-05) so they can't
silently rot. See docs/superpowers/specs/2026-06-06-tile1-greek-flow-delta-design.md.

Fixture: tests/fixtures/uw_greek_flow_SPY.json (405 per-minute rows, captured live).
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from server import greek_flow

_FIXTURE = Path(__file__).parent / "fixtures" / "uw_greek_flow_SPY.json"


@pytest.fixture(scope="module")
def gf():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


# ── Blocker #2: curve population (cumsum non-degenerate) ──────────────────────
def test_series_is_per_minute_and_parses(gf):
    s = greek_flow.series(gf, "dir_delta_flow")
    assert len(s) >= 400                      # ~per-minute over a session
    assert all(isinstance(v, float) for v in s)


def test_cumsum_non_degenerate(gf):
    """The accumulation curve must carry real signal — not empty, not flat. A
    degenerate payload renders 'unavailable', never a silent permanent 'flat'."""
    cd = greek_flow.cumdelta(gf, "dir_delta_flow")
    assert len(cd) >= 400
    assert max(cd) != min(cd)                  # the curve actually moves
    assert not greek_flow.is_degenerate(gf, "dir_delta_flow")


def test_is_degenerate_flags_empty_and_flat():
    assert greek_flow.is_degenerate({"data": []})
    assert greek_flow.is_degenerate({"data": [{"dir_delta_flow": "0"}, {"dir_delta_flow": "0"}]})


# ── Blocker #1: sign convention, anchored to a KNOWN-DIRECTION event ──────────
# 19:32 UTC (3:32 PM ET) on 2026-06-05 had ~$6.6M of ask-side PUT buying (727P
# $4.6M + 719P $2.0M, both at ask) — unambiguously bearish (buying negative-delta
# puts). The field that captures the minute's net delta, total_delta_flow, must be
# NEGATIVE there → pins "negative = bearish, positive = bullish" deterministically,
# on any session (no need for a clean one-sided day). Convention is shared by the
# whole field family (dir_/total_).
def test_sign_convention_negative_on_known_bearish_minute(gf):
    total = greek_flow.value_at_minute(gf, "19:32", "total_delta_flow")
    assert total is not None and total < 0    # bearish event → negative delta → +=bullish


# Regression lock on the directional bucket's DISTINCT behavior: at that same minute
# dir_delta_flow is POSITIVE — the directional bets netted bullish even though the
# headline prints were bearish puts (~20% of the minute's volume). This is NOT a
# sign error; it documents that dir_delta_flow ≠ the tape, so future code that
# reinterprets the field trips this guard.
def test_dir_delta_diverges_from_tape_at_event_minute(gf):
    dir_d = greek_flow.value_at_minute(gf, "19:32", "dir_delta_flow")
    total = greek_flow.value_at_minute(gf, "19:32", "total_delta_flow")
    assert dir_d is not None and total is not None
    assert dir_d > 0 and total < 0             # directional bullish vs total bearish — they disagree
