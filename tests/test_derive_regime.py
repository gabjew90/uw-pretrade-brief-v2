"""derive_regime tests (Phase 4) — market-wide posture, NEVER a direction. Pure: `now`
injected via canon. Golden feeds the real econ-calendar events list.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

NOW = datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc)

from server.pipeline.derive import derive_regime

EVENTS_FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "economic-calendar" / "SPY.json"


def _r(**over):
    base = dict(gamma={"sign": "NEG", "status": "ok"}, vol={"iv": 0.18, "rv": 0.20},
                events=[], tide={"lean": "neutral"}, opex=False, now=NOW)
    base.update(over)
    return {"regime": base}


# ── gamma posture ─────────────────────────────────────────────────────────────
def test_neg_gamma_is_favorable_base():
    assert derive_regime(_r(gamma={"sign": "NEG", "status": "ok"})).posture == "Favorable"


def test_pos_gamma_stands_down_base():
    # POS gamma base is Stand down, but cheap vol + non-hostile tide lifts to Mixed
    rg = derive_regime(_r(gamma={"sign": "POS", "status": "ok"}, vol={"iv": 0.18, "rv": 0.20}))
    assert rg.posture == "Mixed"


def test_pos_gamma_pure_stand_down_when_vol_not_cheap():
    rg = derive_regime(_r(gamma={"sign": "POS", "status": "ok"}, vol={"iv": 0.30, "rv": 0.20}))
    assert rg.posture == "Stand down"


def test_gamma_unavailable_is_mixed():
    assert derive_regime(_r(gamma={"sign": None, "status": "unavailable"})).posture == "Mixed"


# ── event veto ────────────────────────────────────────────────────────────────
def test_high_impact_event_within_1d_vetoes_to_stand_down():
    soon = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)   # < 1 day out
    rg = derive_regime(_r(events=[{"event": "CPI", "time": soon.isoformat()}]))
    assert rg.event_severity == "veto"
    assert rg.posture == "Stand down"          # veto overrides the Favorable base
    assert rg.event_within_hold is True


def test_event_in_a_few_days_warns_and_softens_favorable():
    later = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)  # ~3 days out
    rg = derive_regime(_r(events=[{"event": "FOMC Rate Decision", "type": "fomc", "time": later.isoformat()}]))
    assert rg.event_severity == "warn"
    assert rg.event_within_hold is True
    assert rg.posture == "Mixed"               # Favorable softened by the looming event


def test_low_impact_event_ignored():
    later = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    rg = derive_regime(_r(events=[{"event": "Retail Inventories", "time": later.isoformat()}]))
    assert rg.event_within_hold is False
    assert rg.posture == "Favorable"


def test_event_beyond_hold_window_ignored():
    far = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)   # > 5 days
    rg = derive_regime(_r(events=[{"event": "CPI", "time": far.isoformat()}]))
    assert rg.event_within_hold is False


# ── structural guarantees ─────────────────────────────────────────────────────
def test_regime_is_never_a_direction():
    rg = derive_regime(_r())
    assert not hasattr(rg, "direction")
    assert rg.posture in ("Favorable", "Mixed", "Stand down")   # posture, not calls/puts


def test_missing_now_is_unavailable_mixed():
    rg = derive_regime({"regime": {}})
    assert rg.posture == "Mixed"
    assert rg.provenance.quality.value == "unavailable"


# ── golden: real econ-calendar feeds without crashing ─────────────────────────
def test_golden_real_econ_calendar():
    events = json.loads(EVENTS_FIXTURE.read_text(encoding="utf-8")).get("data", [])
    rg = derive_regime(_r(events=events))
    assert rg.posture in ("Favorable", "Mixed", "Stand down")
    assert isinstance(rg.event_within_hold, bool)
