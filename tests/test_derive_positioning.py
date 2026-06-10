"""derive_positioning tests (list item 2) — per-CONTRACT daily OI history (the deep
source: option-contract/{id}/historic), summed across the flow cluster, trended over the
last _POS_WINDOW sessions. 'unconfirmed' never blocks; positioning is NOT a direction.
"""
import json
from pathlib import Path

from server.models import ContractOIBar, Quality
from server.pipeline.derive import _POS_WINDOW, derive_positioning
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import normalize

FIXTURE = (Path(__file__).parent / "fixtures" / "bronze" / "option-contract-historic"
           / "SPY260717P00710000.json")


def _bars(*oi_by_day, start=1):
    return [ContractOIBar(date=f"2026-06-{start + i:02d}", open_interest=oi)
            for i, oi in enumerate(oi_by_day)]


def _canon(side="call", contracts=None, strikes=(600.0,)):
    return {"flow_side": side, "flow_strikes": list(strikes),
            "contract_oi": contracts if contracts is not None else []}


# ── build / flat / unwind on the summed cluster ───────────────────────────────
def test_growing_cluster_is_building():
    p = derive_positioning(_canon(contracts=[_bars(1000, 1100, 1300)]))
    assert p.confirmation == "building"
    assert p.oi_trend_pct == 30.0
    assert p.side == "call"


def test_shrinking_cluster_is_unwinding():
    p = derive_positioning(_canon(side="put", contracts=[_bars(2000, 1500, 1400)]))
    assert p.confirmation == "unwinding"


def test_stable_cluster_is_flat():
    p = derive_positioning(_canon(contracts=[_bars(1000, 1010, 1020)]))
    assert p.confirmation == "flat"


def test_cluster_sums_across_contracts_per_date():
    a = _bars(500, 500, 500)
    b = _bars(500, 600, 800)          # only this contract builds → summed +30%
    p = derive_positioning(_canon(contracts=[a, b]))
    assert p.confirmation == "building"
    assert p.oi_trend_pct == 30.0


def test_trend_reads_only_the_last_window_sessions():
    # 10 days: ancient collapse then a flat tail — the window must see only the tail
    bars = _bars(9000, 8000, 7000, 6000, 5000, 1000, 1000, 1000, 1000, 1000)
    p = derive_positioning(_canon(contracts=[bars]))
    assert p.confirmation == "flat"            # last _POS_WINDOW days are flat
    assert _POS_WINDOW == 5


def test_contract_birth_does_not_fake_building():
    """A weekly LISTED mid-window has no early bars. Raw summing would read its birth as
    OI 'building' (live-caught). Only the common-coverage window counts."""
    old = _bars(1000, 1000, 1000, 1000, 1000, start=1)        # 06-01..06-05, flat
    young = [                                                  # listed 06-04
        *(_bars(500, 500, start=4)),                           # 06-04, 06-05
    ]
    p = derive_positioning(_canon(contracts=[old, young]))
    # common window starts 06-04: totals 1500, 1500 → FLAT (not +50% from the birth)
    assert p.confirmation == "flat"
    assert all(pt["date"] >= "2026-06-04" for pt in p.oi_series)


# ── unconfirmed never blocks ──────────────────────────────────────────────────
def test_no_history_is_unconfirmed():
    p = derive_positioning(_canon(contracts=[]))
    assert p.confirmation == "unconfirmed"
    assert p.provenance.quality == Quality.UNAVAILABLE


def test_single_day_is_unconfirmed():
    p = derive_positioning(_canon(contracts=[_bars(1000)]))
    assert p.confirmation == "unconfirmed"


def test_no_side_is_unconfirmed():
    p = derive_positioning({"flow_strikes": [600.0], "contract_oi": [_bars(1, 2)]})
    assert p.confirmation == "unconfirmed"


def test_zero_prior_oi_is_unconfirmed():
    p = derive_positioning(_canon(contracts=[_bars(0, 500)]))
    assert p.confirmation == "unconfirmed"


def test_positioning_is_not_a_direction():
    p = derive_positioning(_canon(contracts=[_bars(1000, 1300)]))
    assert not hasattr(p, "direction")


# ── golden: real per-contract history → normalize → derive ───────────────────
def test_golden_real_contract_history():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = RawRecord(endpoint="/option-contract/SPY260717P00710000/historic", params={},
                    ticker="SPY260717P00710000", fetched_at="2026-06-09T15:00:00Z",
                    content_hash="h", payload=payload)
    bars = normalize(raw)
    assert len(bars) == 61                      # the contract's whole life, no 7-day ceiling
    assert bars[0].date < bars[-1].date         # oldest → newest
    assert bars[-1].open_interest > 0           # sane non-None settled OI
    p = derive_positioning({"flow_side": "put", "flow_strikes": [710.0],
                            "contract_oi": [bars]})
    assert p.confirmation in ("building", "flat", "unwinding")
    assert p.oi_trend_pct is not None
