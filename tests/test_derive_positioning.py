"""derive_positioning tests (Phase 4) — cluster OI trend across settled sessions, anchored
to the flow side (tile2-confirmation-principle). 'unconfirmed' never blocks; positioning is
NOT a direction. Golden: the real oi-per-strike fixture normalizes; multi-session build is
synthetic (we only captured one live session; date= history assembly is integration work).
"""
import json
from pathlib import Path

from server.models import OISnapshot, Quality
from server.pipeline.derive import derive_positioning
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import normalize

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "oi-per-strike" / "SPY.json"


def _oi(strike, call_oi=0, put_oi=0):
    return OISnapshot(date="2026-06-05", strike=strike, call_oi=call_oi, put_oi=put_oi)


def _sessions(side, *totals):
    """Build len(totals) settled sessions, each one snapshot at strike 600 with the
    given side OI = total."""
    out = []
    for t in totals:
        out.append([_oi(600.0, call_oi=t if side == "call" else 0,
                         put_oi=t if side == "put" else 0)])
    return out


# ── build / flat / unwind ─────────────────────────────────────────────────────
def test_growing_cluster_is_building():
    p = derive_positioning({"flow_side": "call", "flow_strikes": [600.0],
                            "oi_sessions": _sessions("call", 1000, 1100, 1300)})  # +30%
    assert p.confirmation == "building"
    assert p.oi_trend_pct == 30.0
    assert p.side == "call"


def test_shrinking_cluster_is_unwinding():
    p = derive_positioning({"flow_side": "put", "flow_strikes": [600.0],
                            "oi_sessions": _sessions("put", 2000, 1500, 1400)})  # -30%
    assert p.confirmation == "unwinding"


def test_stable_cluster_is_flat():
    p = derive_positioning({"flow_side": "call", "flow_strikes": [600.0],
                            "oi_sessions": _sessions("call", 1000, 1010, 1020)})  # +2%
    assert p.confirmation == "flat"


def test_side_selects_correct_oi():
    """A call-side anchor must read call_oi, ignoring a put_oi spike at the same strike."""
    sessions = [[_oi(600.0, call_oi=1000, put_oi=50)],
                [_oi(600.0, call_oi=1000, put_oi=9999)]]   # only puts moved
    p = derive_positioning({"flow_side": "call", "flow_strikes": [600.0], "oi_sessions": sessions})
    assert p.confirmation == "flat"                        # call OI unchanged


def test_only_cluster_strikes_counted():
    sessions = [[_oi(600.0, call_oi=1000), _oi(700.0, call_oi=100)],
                [_oi(600.0, call_oi=1000), _oi(700.0, call_oi=9999)]]  # 700 not in cluster
    p = derive_positioning({"flow_side": "call", "flow_strikes": [600.0], "oi_sessions": sessions})
    assert p.confirmation == "flat"                        # the 700 spike is ignored


# ── unconfirmed never blocks ──────────────────────────────────────────────────
def test_single_session_is_unconfirmed():
    p = derive_positioning({"flow_side": "call", "flow_strikes": [600.0],
                            "oi_sessions": _sessions("call", 1000)})
    assert p.confirmation == "unconfirmed"
    assert p.provenance.quality == Quality.UNAVAILABLE


def test_no_side_is_unconfirmed():
    p = derive_positioning({"flow_strikes": [600.0], "oi_sessions": _sessions("call", 1, 2)})
    assert p.confirmation == "unconfirmed"


def test_no_history_is_unconfirmed():
    assert derive_positioning({}).confirmation == "unconfirmed"


def test_zero_prior_oi_is_unconfirmed():
    p = derive_positioning({"flow_side": "call", "flow_strikes": [600.0],
                            "oi_sessions": _sessions("call", 0, 500)})
    assert p.confirmation == "unconfirmed"     # can't compute a % from a zero base


def test_positioning_is_not_a_direction():
    p = derive_positioning({"flow_side": "call", "flow_strikes": [600.0],
                            "oi_sessions": _sessions("call", 1000, 1300)})
    assert not hasattr(p, "direction")         # confirms a side, never picks one


# ── golden: real oi-per-strike normalizes ─────────────────────────────────────
def test_golden_oi_normalizes_and_one_session_is_unconfirmed():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = RawRecord(endpoint="/stock/SPY/oi-per-strike", params={}, ticker="SPY",
                    fetched_at="2026-06-08T15:05:00Z", content_hash="h", payload=payload)
    snaps = normalize(raw)
    assert len(snaps) == 499
    assert snaps[0].call_oi >= 0 and snaps[0].put_oi >= 0
    # one live session alone → unconfirmed (multi-session history is integration work)
    cluster = [s.strike for s in snaps[:3]]
    p = derive_positioning({"flow_side": "call", "flow_strikes": cluster, "oi_sessions": [snaps]})
    assert p.confirmation == "unconfirmed"
