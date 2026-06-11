"""derive_conviction tests (Phase 4) — golden (real greek-flow bronze → normalize →
derive) + sign-invariant property tests. Sign was PINNED in Phase 2 (positive
dir_delta_flow net = calls/bullish); these lock it as a regression guard.
"""
import json
from pathlib import Path

from server.models import GreekFlowPoint, OhlcBar, Provenance, Quality
from server.pipeline.derive import derive_conviction
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import normalize

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "greek-flow" / "SPY.json"


def _pt(ts, dir_delta, total=None):
    return GreekFlowPoint(timestamp=ts, dir_delta_flow=dir_delta, total_delta_flow=total)


def _series(*vals):
    return [_pt(f"2026-06-08T{13 + i // 60:02d}:{i % 60:02d}:00Z", v) for i, v in enumerate(vals)]


# ── sign invariants (the pinned convention) ───────────────────────────────────
def test_positive_net_is_calls():
    f = derive_conviction({"greek_flow": _series(100.0, 200.0, 50.0)})
    assert f.direction == "calls"            # positive dir_delta net = bullish (pinned)
    assert f.dir_delta == 350


def test_negative_net_is_puts():
    f = derive_conviction({"greek_flow": _series(-100.0, -200.0, 50.0)})
    assert f.direction == "puts"             # negative net = bearish
    assert f.dir_delta == -250


def test_flat_series_is_unavailable():
    f = derive_conviction({"greek_flow": _series(5.0, 5.0, 5.0)})    # all equal
    assert f.direction is None
    assert f.provenance.quality == Quality.UNAVAILABLE


def test_empty_is_unavailable():
    f = derive_conviction({"greek_flow": []})
    assert f.direction is None and f.provenance.quality == Quality.UNAVAILABLE


def test_missing_key_is_unavailable():
    assert derive_conviction({}).direction is None


def test_net_zero_but_moving_is_unavailable_lean():
    """A curve that nets to exactly zero has no directional lean → unavailable, even
    though it isn't flat (so accumulation is still read)."""
    f = derive_conviction({"greek_flow": _series(100.0, -100.0, 50.0, -50.0)})
    assert f.direction is None
    assert f.provenance.quality == Quality.UNAVAILABLE


def test_clean_one_way_curve_builds():
    f = derive_conviction({"greek_flow": _series(100.0, 100.0, 100.0, 100.0)})
    # all equal would be flat; make it monotonic-ish via distinct values
    g = derive_conviction({"greek_flow": _series(100.0, 120.0, 110.0, 130.0)})
    assert g.accumulation == "building"      # cumsum marches one way
    assert g.efficiency >= 0.7


def test_reversed_curve_detected():
    f = derive_conviction({"greek_flow": _series(500.0, -300.0, -400.0)})  # cum: 500,200,-200
    assert f.accumulation == "reversed"


def test_provenance_carried_worst_case():
    p = Provenance(quality=Quality.DEGRADED, as_of="2026-06-08T15:00:00Z")
    pts = [_pt("t1", 100.0), _pt("t2", 200.0)]
    for pt in pts:
        pt.provenance = p
    f = derive_conviction({"greek_flow": pts})
    assert f.provenance.quality == Quality.DEGRADED


# ── vol_ratio: |net delta| sized against the day's RTH share volume ───────────
def _ohlc(ts, vol, market_time="r"):
    return OhlcBar(start_time=ts, open=100.0, high=101.0, low=99.0, close=100.5,
                   volume=vol, market_time=market_time)


def test_vol_ratio_sizes_net_against_session_share_volume():
    bars = [_ohlc("2026-06-08T13:30:00Z", 6000), _ohlc("2026-06-08T13:45:00Z", 4000)]
    f = derive_conviction({"greek_flow": _series(100.0, 200.0, 50.0), "ohlc": bars})
    assert f.share_volume == 10000.0
    assert f.vol_ratio == 0.035                  # |350| / 10000


def test_vol_ratio_excludes_prior_session_and_extended_hours():
    bars = [
        _ohlc("2026-06-05T14:00:00Z", 90000),                       # prior session
        _ohlc("2026-06-08T11:00:00Z", 50000, market_time="pr"),     # pre-market
        _ohlc("2026-06-08T13:30:00Z", 7000),
        _ohlc("2026-06-08T23:45:00Z", 50000, market_time="po"),     # post-market
    ]
    f = derive_conviction({"greek_flow": _series(-100.0, -200.0, 50.0), "ohlc": bars})
    assert f.share_volume == 7000.0
    assert f.vol_ratio == round(250 / 7000, 4)   # sized on |net|, sign-free


def test_vol_ratio_none_without_ohlc():
    f = derive_conviction({"greek_flow": _series(100.0, 200.0, 50.0)})
    assert f.vol_ratio is None and f.share_volume is None
    f2 = derive_conviction({"greek_flow": _series(100.0, 200.0, 50.0), "ohlc": []})
    assert f2.vol_ratio is None


# ── golden: real greek-flow bronze → normalize → derive ───────────────────────
def test_golden_real_greek_flow():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = RawRecord(endpoint="/stock/SPY/greek-flow", params={}, ticker="SPY",
                    fetched_at="2026-06-08T15:05:00Z", content_hash="h", payload=payload)
    pts = normalize(raw)
    assert len(pts) > 50                      # a real RTH session, ~94 minutes
    f = derive_conviction({"greek_flow": pts})
    assert f.direction in ("calls", "puts")   # sane non-None directional read
    # SPY 6/8 dir_delta_flow netted NEGATIVE (Phase-2 finding) → puts
    assert f.direction == "puts"
    assert f.dir_delta < 0
