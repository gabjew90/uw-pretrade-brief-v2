"""derive_dealer_gamma tests (Phase 4) — golden (real spot-exposures → normalize →
derive) + the signed-sum flip invariant (the 2026-05-30 GEX sign bug: net = call+put,
put pre-signed negative; subtracting breaks the flip).
"""
import json
from pathlib import Path

from server.models import GammaStrike, Provenance, Quality
from server.pipeline.derive import derive_dealer_gamma
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import normalize

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "spot-exposures-strike" / "SPY.json"


def _g(strike, call_oi, put_oi, price=100.0):
    return GammaStrike(strike=strike, call_gamma_oi=call_oi, put_gamma_oi=put_oi, price=price)


# ── signed-sum + flip invariants ──────────────────────────────────────────────
def test_net_is_signed_sum_put_pre_signed_negative():
    """Below spot net positive, above spot net negative → cumulative flips once.
    With a SUBTRACTION (the old bug) puts would double-negate and no flip would appear."""
    rungs = [
        _g(90, 100.0, -10.0, price=100.0),   # net +90
        _g(95, 80.0, -20.0, price=100.0),    # net +60  (cum +150)
        _g(105, 10.0, -200.0, price=100.0),  # net -190 (cum -40)  → crossing here
        _g(110, 5.0, -150.0, price=100.0),   # net -145 (cum -185)
    ]
    dg = derive_dealer_gamma({"gamma_strikes": rungs})
    assert dg.flip_status == "ok"
    # flip strike is 105 → flip_pct = (105-100)/100*100 = +5%
    assert round(dg.flip_pct, 1) == 5.0
    assert dg.gex_sign == "NEG"              # aggregate net negative


def test_flip_picks_crossing_nearest_spot():
    rungs = [
        _g(80, 100.0, -10.0, price=100.0),   # +90  cum +90
        _g(85, 10.0, -200.0, price=100.0),   # -190 cum -100  → crossing @85 (far)
        _g(95, 300.0, -10.0, price=100.0),   # +290 cum +190  → crossing @95 (near)
        _g(120, 10.0, -300.0, price=100.0),  # -290 cum -100  → crossing @120 (far)
    ]
    dg = derive_dealer_gamma({"gamma_strikes": rungs})
    # crossings at 85, 95, 120; nearest spot(100) is 95 → flip_pct = -5%
    assert round(dg.flip_pct, 1) == -5.0


def test_no_crossing_is_no_flip():
    rungs = [_g(90, 100.0, -5.0, price=100.0), _g(110, 80.0, -5.0, price=100.0)]  # all net positive
    dg = derive_dealer_gamma({"gamma_strikes": rungs})
    assert dg.flip_status == "no_flip"
    assert dg.gex_sign == "POS"


def test_walls_call_above_put_below():
    rungs = [
        _g(90, 10.0, -500.0, price=100.0),   # biggest |put| below → put wall @90
        _g(95, 20.0, -100.0, price=100.0),
        _g(110, 800.0, -10.0, price=100.0),  # biggest call above → call wall @110
        _g(120, 50.0, -10.0, price=100.0),
    ]
    dg = derive_dealer_gamma({"gamma_strikes": rungs})
    assert round(dg.call_wall_pct, 1) == 10.0    # (110-100)/100
    assert round(dg.put_wall_pct, 1) == 10.0     # (100-90)/100


def test_empty_is_unavailable():
    dg = derive_dealer_gamma({"gamma_strikes": []})
    assert dg.flip_status == "unavailable"
    assert dg.provenance.quality == Quality.UNAVAILABLE


def test_no_spot_price_is_unavailable():
    rungs = [GammaStrike(strike=100, call_gamma_oi=1.0, put_gamma_oi=-1.0, price=None)]
    dg = derive_dealer_gamma({"gamma_strikes": rungs})
    assert dg.flip_status == "unavailable"


# ── band guard: a one-sided strike band is UNTRUSTWORTHY, not a reading ───────
def test_one_sided_band_is_unavailable():
    """Below-spot-only coverage would invert gex_sign and fake the call wall at the band
    edge (review SEVERE #1) — must read unavailable, never a confident wrong EXTEND."""
    rungs = [_g(k, 10.0, -50.0, price=100.0) for k in (70, 80, 90, 95, 100)]  # nothing above
    dg = derive_dealer_gamma({"gamma_strikes": rungs})
    assert dg.flip_status == "unavailable"
    assert "one-sided" in dg.provenance.note


def test_canon_spot_preferred_over_rung_price():
    rungs = [_g(90, 100.0, -10.0, price=None), _g(110, 10.0, -200.0, price=None)]
    dg = derive_dealer_gamma({"gamma_strikes": rungs, "spot": 100.0})
    assert dg.flip_status in ("ok", "no_flip")     # spot came from canon


# ── golden: real spot-exposures bronze → normalize → derive ───────────────────
def test_golden_real_spot_exposures_default_band_is_rejected():
    """The captured fixture used UW's DEFAULT band: strikes 525→750 with spot 744.94 =
    -29.5%/+0.68% — one-sided. The guard must refuse it (this band inverted gex_sign to
    NEG and put the 'call wall' at the band edge before the fix)."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = RawRecord(endpoint="/stock/SPY/spot-exposures/strike", params={}, ticker="SPY",
                    fetched_at="2026-06-08T15:05:00Z", content_hash="h", payload=payload)
    rungs = normalize(raw)
    assert len(rungs) == 50
    dg = derive_dealer_gamma({"gamma_strikes": rungs})
    assert dg.flip_status == "unavailable"
    assert "one-sided" in dg.provenance.note
