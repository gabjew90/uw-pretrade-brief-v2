"""15m candles (price axis for the chart UI): golden parse, regular-hours newest-session
filter, and the price element. Price is CONTEXT — it never feeds the verdict.
"""
import json
from pathlib import Path

from server.models import OhlcBar
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import normalize
from server.pipeline.orchestrate import session_candles

FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "ohlc-15m" / "SPY.json"


def _bar(ts, market_time="r", o=100.0, c=101.0):
    return OhlcBar(start_time=ts, open=o, high=max(o, c) + 0.5, low=min(o, c) - 0.5,
                   close=c, volume=10, market_time=market_time)


def test_golden_ohlc_parses():
    raw = RawRecord(endpoint="/stock/SPY/ohlc/15m", params={}, ticker="SPY",
                    fetched_at="t", content_hash="h",
                    payload=json.loads(FIXTURE.read_text(encoding="utf-8")))
    bars = normalize(raw)
    assert len(bars) == 2500
    assert bars[0].start_time < bars[-1].start_time        # oldest -> newest
    assert bars[-1].close > 0 and bars[-1].high >= bars[-1].low


def test_session_candles_regular_hours_newest_session_only():
    bars = [
        _bar("2026-06-09T14:00:00Z"),                      # prior session — dropped
        _bar("2026-06-10T13:30:00Z"),                      # newest, RTH
        _bar("2026-06-10T14:00:00Z"),
        _bar("2026-06-10T23:45:00Z", market_time="po"),    # post-market — dropped
        _bar("2026-06-10T11:00:00Z", market_time="pr"),    # pre-market — dropped
    ]
    pts = session_candles(bars)
    assert len(pts) == 2
    assert pts[0]["t"] == "09:30" and pts[1]["t"] == "10:00"   # ET times
    assert {"t", "o", "h", "l", "c", "v"} <= set(pts[0])


def test_session_candles_empty():
    assert session_candles([]) == []
    assert session_candles(None) == []


def test_price_element_in_viewmodel():
    from server.pipeline.present import present
    from server.pipeline.decide import decide
    from server.models import Flow
    sigs = {"flow": Flow(direction="puts", direction_basis="opening_flow",
                         call_prem=1.0, put_prem=2.0)}
    pts = [{"t": "09:30", "o": 100.0, "h": 101.0, "l": 99.5, "c": 100.5, "v": 10},
           {"t": "09:45", "o": 100.5, "h": 102.0, "l": 100.4, "c": 101.8, "v": 12}]
    vm = present("SPY", sigs, decide(sigs), candles=pts)
    el = vm.elements[0]
    assert el.key == "price"
    assert el.surface == "$101.8"
    assert "+1.80% session" in el.meaning
    assert el.series == {"kind": "candles", "points": pts}
    assert el.detail["High"] == "$102"
    # and absent candles -> no price element
    vm2 = present("SPY", sigs, decide(sigs))
    assert all(e.key != "price" for e in vm2.elements)
