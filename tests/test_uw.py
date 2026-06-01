"""Tests for server.uw.

Fixture-based — no network calls. Verifies the loaded fixtures match the shape
the rest of the code expects. A live schema-drift test exists separately
(test_live_schema.py, opt-in via `pytest -m live`).
"""
from __future__ import annotations
import pytest
from server.uw import (
    _unwrap,
    gex_records,
    oi_records,
    flow_records,
    hot_tickers,
    term_structure,
    extract_spot,
    extract_iv_rank,
    max_pain_value,
    darkpool_records,
    darkpool_net_premium,
    next_earnings,
    parse_option_symbol,
    contract_records,
    contracts_near_focus,
)


# ---------- Shape contracts on raw payloads ----------

def test_gex_payload_has_strike_records(gex_strike_spy):
    rows = _unwrap(gex_strike_spy)
    assert isinstance(rows, list) and len(rows) > 0
    sample = rows[0]
    assert "strike" in sample
    assert "call_gamma_oi" in sample and "put_gamma_oi" in sample


def test_oi_payload_has_strike_records(oi_strike_spy):
    rows = _unwrap(oi_strike_spy)
    assert isinstance(rows, list) and len(rows) > 0
    sample = rows[0]
    assert "strike" in sample
    assert "call_oi" in sample and "put_oi" in sample


def test_flow_payload_has_records(flow_alerts_spy):
    rows = _unwrap(flow_alerts_spy)
    assert isinstance(rows, list) and len(rows) > 0
    sample = rows[0]
    assert "type" in sample
    assert "total_premium" in sample
    assert "ticker" in sample


def test_volatility_payload_has_dte_volatility(volatility_spy):
    rows = _unwrap(volatility_spy)
    assert isinstance(rows, list) and len(rows) > 0
    sample = rows[0]
    assert "dte" in sample
    assert "volatility" in sample


def test_max_pain_payload_has_front_week(max_pain_spy):
    rows = _unwrap(max_pain_spy)
    assert isinstance(rows, list) and len(rows) > 0
    first = rows[0]
    assert "max_pain" in first
    assert "expiry" in first


# ---------- Parser outputs ----------

def test_gex_records_sorted_by_strike(gex_strike_spy):
    records = gex_records(gex_strike_spy)
    assert len(records) > 0
    strikes = [r["strike"] for r in records]
    assert strikes == sorted(strikes)
    # Every record must have a numeric gamma (the parser computed call_oi - put_oi)
    for r in records:
        assert isinstance(r["gamma"], float)


def test_tile4_endpoint_wrappers_paths_and_params(monkeypatch):
    """Tile 4's new endpoints hit the right paths with their REQUIRED params
    (greeks/skew need expiry; atm-chains needs expirations[]; skew needs delta)."""
    from server import uw
    seen = []
    monkeypatch.setattr(uw, "_get", lambda path, params=None: seen.append((path, params or {})) or {"data": []})
    uw.fetch_greeks("SPY", "2026-06-05")
    uw.fetch_atm_chains("SPY", ["2026-06-05"])
    uw.fetch_risk_reversal_skew("SPY", "2026-06-05")
    uw.fetch_realized_vol("SPY")
    uw.fetch_stock_state("SPY")
    uw.fetch_fda_calendar("SPY")
    by_path = {p: q for p, q in seen}
    assert by_path["/api/stock/SPY/greeks"]["expiry"] == "2026-06-05"
    assert by_path["/api/stock/SPY/atm-chains"]["expirations[]"] == ["2026-06-05"]
    sk = by_path["/api/stock/SPY/historical_risk_reversal_skew"]
    assert sk["expiry"] == "2026-06-05" and sk.get("delta")
    assert "/api/stock/SPY/volatility/realized" in by_path
    assert "/api/stock/SPY/stock-state" in by_path
    assert by_path["/api/market/fda-calendar"]["ticker"] == "SPY"


def test_fetch_greek_exposure_expiry_hits_right_path(monkeypatch):
    from server import uw
    seen = {}
    monkeypatch.setattr(uw, "_get", lambda path, params=None: seen.update(path=path, params=params) or {"data": []})
    uw.fetch_greek_exposure_expiry("SPY")
    assert seen["path"] == "/api/stock/SPY/greek-exposure/expiry"


def test_fetch_spot_exposures_expiry_strike_sends_expirations_window_limit(monkeypatch):
    from server import uw
    seen = {}
    monkeypatch.setattr(uw, "_get", lambda path, params=None: seen.update(path=path, params=params or {}) or {"data": []})
    uw.fetch_spot_exposures_expiry_strike("SPY", ["2026-06-05"], min_strike=605, max_strike=905)
    assert seen["path"] == "/api/stock/SPY/spot-exposures/expiry-strike"
    assert seen["params"].get("expirations[]") == ["2026-06-05"]   # required, list form
    assert seen["params"].get("limit") == 500
    assert seen["params"].get("min_strike") == 605 and seen["params"].get("max_strike") == 905


def test_spot_exposures_sends_limit_and_strike_window(monkeypatch):
    """UW's default limit returns only ~50 strikes (the lowest in the chain), so
    we must send limit=500 alongside the min/max window to actually get the full
    near-spot coverage. Regression for the band-but-no-limit miss."""
    from server import uw
    seen = {}
    monkeypatch.setattr(uw, "_get", lambda path, params=None: seen.update(params or {}) or {"data": []})
    uw.fetch_spot_exposures_strike("SPY", min_strike=605, max_strike=905)
    assert seen.get("limit") == 500
    assert seen.get("min_strike") == 605 and seen.get("max_strike") == 905


def test_gex_records_net_gamma_is_call_plus_signed_put():
    """UW pre-signs put_gamma_oi negative. Net dealer gamma = call + put (the
    signed sum), NOT call - put. Regression: the subtraction double-negated puts,
    making every strike positive and breaking the gamma-flip computation."""
    payload = {"data": [{"strike": 100, "call_gamma_oi": "1.0e8", "put_gamma_oi": "-3.0e8"}]}
    recs = gex_records(payload)
    assert recs[0]["gamma"] == pytest.approx(-2.0e8)  # puts dominate → net negative


def test_oi_records_sorted_by_strike(oi_strike_spy):
    recs = oi_records(oi_strike_spy)
    assert len(recs) > 0
    assert [r["strike"] for r in recs] == sorted(r["strike"] for r in recs)
    for r in recs:
        assert isinstance(r["call_oi"], int)
        assert isinstance(r["put_oi"], int)


def test_flow_records_returns_list_with_sides_and_premiums(flow_alerts_spy):
    records = flow_records(flow_alerts_spy)
    assert isinstance(records, list)
    assert len(records) > 0
    for r in records[:5]:
        assert r["side"] in ("call", "put"), f"unexpected side: {r['side']}"
        assert isinstance(r["premium_usd"], float)
        assert r["premium_usd"] >= 0


def test_hot_tickers_returns_unique_symbols(hot_today):
    tickers = hot_tickers(hot_today, limit=15)
    assert isinstance(tickers, list)
    assert len(tickers) == len(set(tickers))
    assert len(tickers) > 0


def test_term_structure_sorted_by_dte(volatility_spy):
    ts = term_structure(volatility_spy)
    assert len(ts) > 0
    assert [r["dte"] for r in ts] == sorted(r["dte"] for r in ts)
    for r in ts:
        assert isinstance(r["dte"], int)
        assert isinstance(r["iv"], float)
        assert 0 < r["iv"] < 5  # IV expressed as decimal (0.25 = 25%)


# ---------- Scalar extractors ----------

def test_extract_spot_finds_from_flow_underlying_price(flow_alerts_spy, max_pain_spy):
    # extract_spot accepts variadic payloads — try flow first (has underlying_price),
    # fall through to max_pain (has close).
    spot = extract_spot(flow_alerts_spy, max_pain_spy)
    assert spot is not None
    assert spot > 0


def test_extract_spot_returns_none_for_empty():
    assert extract_spot(None, {"data": []}) is None


def test_max_pain_value_returns_front_week_strike(max_pain_spy):
    mp = max_pain_value(max_pain_spy)
    assert mp is not None
    assert mp > 0


def test_extract_iv_rank_returns_none_when_absent(volatility_spy):
    # /volatility/term-structure response doesn't include IV rank
    assert extract_iv_rank(volatility_spy) is None


def test_extract_iv_rank_uses_interpolated_iv_when_provided(volatility_spy, interpolated_iv_spy):
    # /interpolated-iv has a 'percentile' field per DTE row; we pick front-week
    rank = extract_iv_rank(volatility_spy, interpolated_iv_spy)
    assert rank is not None
    assert 0 <= rank <= 100  # normalized to 0-100 scale


# ---------- New endpoints: dark pool, earnings ----------

def test_darkpool_records_classify_by_nbbo_midpoint(darkpool_spy):
    records = darkpool_records(darkpool_spy)
    assert len(records) > 0
    valid_sides = {"buy", "sell", "neutral"}
    for r in records:
        assert r["side"] in valid_sides
        assert isinstance(r["price"], float) and r["price"] > 0
        assert isinstance(r["premium"], float)
        assert isinstance(r["size"], int)


def test_darkpool_net_premium_returns_signed_value(darkpool_spy):
    records = darkpool_records(darkpool_spy)
    net = darkpool_net_premium(records)
    assert isinstance(net, float)


def test_darkpool_net_premium_synthetic():
    """Pure unit test on synthetic records."""
    recs = [
        {"side": "buy", "premium": 1_000_000, "ts": "", "price": 0, "size": 0, "raw": {}},
        {"side": "sell", "premium": 300_000, "ts": "", "price": 0, "size": 0, "raw": {}},
        {"side": "neutral", "premium": 500_000, "ts": "", "price": 0, "size": 0, "raw": {}},
    ]
    assert darkpool_net_premium(recs) == 700_000.0


def test_next_earnings_returns_none_for_etf(earnings_spy):
    """SPY is an ETF — earnings list is empty, parser returns None."""
    assert next_earnings(earnings_spy) is None


def test_next_earnings_synthetic_picks_nearest_future():
    """Synthetic: with multiple dates, pick the nearest future one."""
    import datetime as _dt
    future_near = (_dt.date.today() + _dt.timedelta(days=10)).isoformat()
    future_far = (_dt.date.today() + _dt.timedelta(days=100)).isoformat()
    past = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
    payload = {"data": [
        {"expected_date": past},
        {"expected_date": future_far},
        {"expected_date": future_near},
    ]}
    result = next_earnings(payload)
    assert result == future_near


# ---------- Budget meter integration ----------

class _FakeResp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self._payload = payload if payload is not None else {"data": []}
        self.text = "err"
        self.headers = headers or {}

    def json(self):
        return self._payload


def test_get_reports_uw_usage_headers_to_budget(monkeypatch):
    """A successful UW response's usage headers are forwarded to the budget meter
    so it can use UW's authoritative count + cap."""
    from server import uw, budget
    budget.reset()
    monkeypatch.setenv("UW_API_KEY", "test-key")
    headers = {"x-uw-daily-req-count": "8200", "x-uw-token-req-limit": "15000",
               "x-uw-req-per-minute-remaining": "117"}
    monkeypatch.setattr(uw.requests, "get",
                        lambda *a, **k: _FakeResp(200, {"data": [1]}, headers=headers))
    uw.fetch_oi_strike("SPY")
    snap = budget.snapshot()
    assert snap["calls_today"] == 8200
    assert snap["daily_cap"] == 15000
    assert snap["source"] == "uw_headers"


def test_get_records_one_budget_call_on_success(monkeypatch):
    from server import uw, budget
    budget.reset()
    monkeypatch.setenv("UW_API_KEY", "test-key")
    monkeypatch.setattr(uw.requests, "get", lambda *a, **k: _FakeResp(200, {"data": [1]}))
    uw.fetch_oi_strike("SPY")
    assert budget.snapshot()["calls_today"] == 1


def test_get_records_every_http_attempt_including_429_retries(monkeypatch):
    """Each retry is a real HTTP hit that consumes quota, so all 3 attempts count."""
    from server import uw, budget
    budget.reset()
    monkeypatch.setenv("UW_API_KEY", "test-key")
    monkeypatch.setattr(uw, "_429_RETRY_DELAYS_S", (0, 0))  # no sleep in test
    seq = [_FakeResp(429), _FakeResp(429), _FakeResp(200, {"data": [1]})]
    monkeypatch.setattr(uw.requests, "get", lambda *a, **k: seq.pop(0))
    uw.fetch_oi_strike("SPY")
    assert budget.snapshot()["calls_today"] == 3


# ---------- Smoke imports ----------

def test_fetch_module_imports():
    """fetch module imports without side effects (proven by other tests)."""
    from server import uw
    for name in (
        "fetch_spot_exposures_strike", "fetch_oi_strike", "fetch_flow_alerts",
        "fetch_volatility", "fetch_max_pain",
        "fetch_darkpool", "fetch_earnings", "fetch_interpolated_iv",
        "fetch_option_contracts",
    ):
        assert hasattr(uw, name), f"missing endpoint method: {name}"


# ---------- Option-contracts parser + contract picker ----------

def test_parse_option_symbol_call():
    parsed = parse_option_symbol("SPY260522C00748000")
    assert parsed == {
        "underlying": "SPY",
        "expiry": "2026-05-22",
        "type": "call",
        "strike": 748.0,
    }


def test_parse_option_symbol_put_fractional_strike():
    parsed = parse_option_symbol("NVDA260530P00449500")
    assert parsed["type"] == "put"
    assert parsed["strike"] == 449.5


def test_parse_option_symbol_garbage_returns_none():
    assert parse_option_symbol("") is None
    assert parse_option_symbol("not-an-option-symbol") is None
    assert parse_option_symbol(None) is None


def test_contract_records_on_spy_fixture(option_contracts_spy):
    records = contract_records(option_contracts_spy)
    assert len(records) > 0
    sample = records[0]
    for k in ("symbol", "expiry", "type", "strike", "bid", "ask", "iv", "volume", "oi"):
        assert k in sample
    assert sample["type"] in ("call", "put")
    assert isinstance(sample["strike"], float)


def test_contracts_near_focus_filters_to_front_week_strikes():
    """Synthetic: contracts spanning two expiries; only the nearest-future
    expiry returned, only ±2 strikes around focus."""
    import datetime as _dt
    front = (_dt.date.today() + _dt.timedelta(days=2)).isoformat()
    far = (_dt.date.today() + _dt.timedelta(days=30)).isoformat()
    records = []
    # Front-week 745, 746, 747, 748, 749, 750, 751 (call + put each)
    for strike in (745.0, 746.0, 747.0, 748.0, 749.0, 750.0, 751.0):
        for t in ("call", "put"):
            records.append({"symbol": "X", "expiry": front, "type": t,
                            "strike": strike, "bid": 1, "ask": 2, "iv": 0.2,
                            "volume": 100, "oi": 200})
    # Far expiry — must be excluded
    records.append({"symbol": "X", "expiry": far, "type": "call",
                    "strike": 748.0, "bid": 1, "ask": 2, "iv": 0.2,
                    "volume": 100, "oi": 200})

    near = contracts_near_focus(records, focus_strike=748.0, n_strikes=2)
    expiries = set(r["expiry"] for r in near)
    assert expiries == {front}, "far-expiry contract must be filtered out"
    strikes = sorted(set(r["strike"] for r in near))
    # ±2 around 748 → 746, 747, 748, 749, 750
    assert strikes == [746.0, 747.0, 748.0, 749.0, 750.0]
    # Should have call + put per strike
    assert len(near) == len(strikes) * 2


def test_contracts_near_focus_empty_input():
    assert contracts_near_focus([], focus_strike=500.0) == []


def test_contracts_near_focus_no_future_expiries():
    """All contracts already expired → empty result."""
    import datetime as _dt
    past = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
    records = [{"symbol": "X", "expiry": past, "type": "call",
                "strike": 100.0, "bid": 1, "ask": 2, "iv": 0.2,
                "volume": 100, "oi": 200}]
    assert contracts_near_focus(records, focus_strike=100.0) == []
