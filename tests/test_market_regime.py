from datetime import datetime, timezone, timedelta
from server import market_regime as mr


def _now():
    return datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc)


def _ev(name, days, type_="report", hour=13):
    t = (_now() + timedelta(days=days)).replace(hour=hour, minute=30)
    return {"event": name, "time": t.isoformat(), "type": type_}


def _call(**kw):
    base = dict(gamma={"sign": "NEG", "flip_pct": 1.0, "status": "ok"},
                vol={"iv": 0.18, "rv": 0.20, "trend": None},
                events=[], tide={"lean": "neutral"}, opex=False, now=_now())
    base.update(kw)
    return mr.compute_market_regime(**base)


def test_trend_regime_headline_and_favorable():
    out = _call(gamma={"sign": "NEG", "flip_pct": 1.0, "status": "ok"})
    assert "trend" in out["headline"].lower()
    assert out["posture"] == "Favorable"


def test_chop_regime_stands_down_on_its_own():
    out = _call(gamma={"sign": "POS", "flip_pct": 1.0, "status": "ok"},
                vol={"iv": 0.30, "rv": 0.20, "trend": None}, tide={"lean": "neutral"})
    assert "chop" in out["headline"].lower() or "pinned" in out["headline"].lower()
    assert out["posture"] == "Stand down"


def test_chop_lifts_to_mixed_when_vol_cheap_and_tide_ok():
    out = _call(gamma={"sign": "POS", "flip_pct": 1.0, "status": "ok"},
                vol={"iv": 0.15, "rv": 0.20, "trend": "falling"}, tide={"lean": "neutral"})
    assert out["posture"] == "Mixed"


def test_event_within_1_day_hard_standdown_overrides_trend():
    out = _call(gamma={"sign": "NEG", "flip_pct": 1.0, "status": "ok"},
                events=[_ev("FOMC Rate Decision", 1, type_="fomc")])
    assert out["posture"] == "Stand down"
    assert out["event"]["severity"] == "veto"
    assert out["event_within_hold"] is True


def test_event_2to5_days_warns_and_downgrades_favorable_to_mixed():
    out = _call(gamma={"sign": "NEG", "flip_pct": 1.0, "status": "ok"},
                events=[_ev("CPI", 3)])
    assert out["event"]["severity"] == "warn"
    assert out["posture"] == "Mixed"
    assert out["event_within_hold"] is True


def test_past_event_today_does_not_veto():
    past = {"event": "CPI", "type": "report",
            "time": (_now() - timedelta(hours=2)).isoformat()}
    out = _call(gamma={"sign": "NEG", "flip_pct": 1.0, "status": "ok"}, events=[past])
    assert out["event_within_hold"] is False
    assert out["posture"] == "Favorable"


def test_low_impact_event_is_ignored():
    out = _call(events=[_ev("Consumer sentiment (final)", 1)])
    assert out["event_within_hold"] is False


def test_unknown_gamma_is_conservative_mixed_not_favorable():
    out = _call(gamma={"sign": None, "flip_pct": 0.0, "status": "unavailable"})
    assert out["posture"] in ("Mixed", "Stand down")
    assert "unavailable" in out["headline"].lower()


def test_no_direction_language_anywhere():
    for g in ("NEG", "POS"):
        out = _call(gamma={"sign": g, "flip_pct": 1.0, "status": "ok"},
                    events=[_ev("FOMC", 1, type_="fomc")], tide={"lean": "bull"})
        blob = " ".join(str(v) for v in out.values()).lower()
        for banned in (" buy ", " sell ", "calls", "puts", "market up", "market down", "go long", "go short"):
            assert banned not in blob, f"direction language leaked: {banned!r}"
