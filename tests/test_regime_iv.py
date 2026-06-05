"""Regression: the regime vol line read the wrong interpolated-iv field names
(implied_volatility/iv/iv_rank) when UW actually uses 'volatility' per DTE row,
so SPY IV never populated and the regime vol line was always "unavailable"."""
import json
from pathlib import Path

from server import snapshot

_GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "interpolated_iv.json"


def test_regime_iv_level_reads_volatility_front_week():
    payload = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    iv = snapshot._regime_iv_level(payload)
    # Front-week row in the golden is days=1, volatility="1.248".
    assert iv is not None, "regime IV must populate from the 'volatility' field"
    assert abs(iv - 1.248) < 1e-6


def test_regime_iv_level_none_on_failure_or_empty():
    from server import storage
    assert snapshot._regime_iv_level(storage.UWFailure("x", None, "boom")) is None
    assert snapshot._regime_iv_level({"data": []}) is None
    assert snapshot._regime_iv_level({}) is None
