"""normalize_option_contracts tests (Phase 4) — OCC symbol parsing + NBBO mapping against
the real captured chain. The strike/expiry/type come from `option_symbol`; an OCC-format
break (no symbol parses) raises rather than returning a silently empty chain.
"""
import json
from pathlib import Path

import pytest

from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import NormalizeError, _parse_occ, normalize

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "option-contracts" / "SPY.json"


def _raw(payload):
    return RawRecord(endpoint="/stock/SPY/option-contracts", params={}, ticker="SPY",
                     fetched_at="2026-06-08T15:05:00Z", content_hash="h", payload=payload)


def test_parse_occ_symbol():
    occ = _parse_occ("SPY260608P00740000")
    assert occ == {"type": "put", "expiry": "2026-06-08", "strike": 740.0}
    assert _parse_occ("SPY260522C00748000")["type"] == "call"
    assert _parse_occ("garbage") is None


def test_golden_real_chain_parses():
    chain = normalize(_raw(json.loads(FIXTURE.read_text(encoding="utf-8"))))
    assert len(chain) > 100
    c0 = chain[0]
    assert c0.type in ("call", "put")
    assert c0.strike > 0
    assert c0.ask >= c0.bid                         # NBBO sane
    assert c0.expiry.startswith("20")


def test_golden_greeks_sheet_parses_with_both_legs():
    """The greeks endpoint returns SEPARATE call_/put_ legs per strike (v2 live lore,
    re-pinned by the golden capture)."""
    gpath = Path(__file__).parent / "fixtures" / "bronze" / "greeks" / "SPY.json"
    raw = RawRecord(endpoint="/stock/SPY/greeks", params={"expiry": "2026-07-10"},
                    ticker="SPY", fetched_at="t", content_hash="h",
                    payload=json.loads(gpath.read_text(encoding="utf-8")))
    rows = normalize(raw)
    assert len(rows) == 177
    mid = next(r for r in rows if r.call_delta is not None and 0.3 < r.call_delta < 0.7)
    assert mid.call_theta is not None and mid.call_theta < 0     # long options decay
    assert mid.put_delta is not None and mid.put_delta < 0


def test_unparseable_symbols_skipped_but_all_fail_raises():
    # individual junk skipped:
    good = json.loads(FIXTURE.read_text(encoding="utf-8"))["data"][0]
    mixed = normalize(_raw({"data": [good, {"option_symbol": "JUNK", "nbbo_bid": "1"}]}))
    assert len(mixed) == 1
    # but a payload where NOTHING parses is an OCC-format break → raise
    with pytest.raises(NormalizeError, match="OCC format"):
        normalize(_raw({"data": [{"option_symbol": "JUNK"}]}))
