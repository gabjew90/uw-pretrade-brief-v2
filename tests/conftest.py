"""Shared test fixtures and the `live` marker setup."""
from __future__ import annotations
import json
from pathlib import Path
import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load(name: str):
    return json.loads((FIXTURE_DIR / name).read_text())


@pytest.fixture
def gex_strike_spy():
    """Per-strike Spot GEX for SPY (from /api/stock/SPY/spot-exposures/strike).
    Loader name keeps the short 'gex_strike' label since GEX is conventional."""
    return _load("uw_spot_exposures_strike_SPY.json")


@pytest.fixture
def oi_strike_spy():
    return _load("uw_oi_strike_SPY.json")


@pytest.fixture
def flow_alerts_spy():
    return _load("uw_flow_alerts_SPY.json")


@pytest.fixture
def volatility_spy():
    return _load("uw_volatility_SPY.json")


@pytest.fixture
def max_pain_spy():
    return _load("uw_max_pain_SPY.json")


@pytest.fixture
def hot_today():
    return _load("uw_hot_today.json")


@pytest.fixture
def darkpool_spy():
    return _load("uw_darkpool_SPY.json")


@pytest.fixture
def earnings_spy():
    """SPY is an ETF; earnings list is expected empty. Use for empty-case tests."""
    return _load("uw_earnings_SPY.json")


@pytest.fixture
def interpolated_iv_spy():
    return _load("uw_interpolated_iv_SPY.json")


@pytest.fixture
def option_contracts_spy():
    return _load("uw_option_contracts_SPY.json")


def pytest_collection_modifyitems(config, items):
    """Skip @pytest.mark.live tests unless `-m live` was passed."""
    selected_marker = config.getoption("-m") or ""
    if "live" in selected_marker:
        return
    skip_live = pytest.mark.skip(reason="live; run with `pytest -m live`")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
