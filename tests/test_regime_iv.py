"""Regime vol read: source IV+RV from /volatility/realized (a daily IV+RV series),
NOT the spiky interpolated_iv front-week. iv = latest row's implied_volatility
(populated even before today's RV settles); rv = latest non-null realized_volatility.
This is a matched IV/RV pair from one endpoint (one fetch)."""
import json
from pathlib import Path

from server import snapshot, storage

_GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "realized_vol.json"


def test_regime_vol_iv_latest_rv_latest_settled():
    payload = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    iv, rv = snapshot._regime_vol(payload)
    assert abs(iv - 0.164) < 1e-9        # latest row's implied_volatility (today)
    assert abs(rv - 0.105) < 1e-9        # latest NON-null realized_volatility (06-02)


def test_regime_vol_none_on_failure_or_empty():
    assert snapshot._regime_vol(storage.UWFailure("x", None, "boom")) == (None, None)
    assert snapshot._regime_vol({"data": []}) == (None, None)
    assert snapshot._regime_vol({}) == (None, None)
