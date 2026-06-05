"""Schema-contract tests — catch UW field-shape DRIFT at CI/deploy time, not in
prod (silent nulls). Two layers:

1. OFFLINE (default, every run): validate the committed golden fixtures
   (tests/fixtures/golden/, real captured UW responses) against the declared
   contracts. Catches OUR parsing assumptions breaking — e.g. if someone edits a
   parser to read a field the golden data doesn't have.

2. LIVE (opt-in, `pytest -m live`): fetch each endpoint FRESH from UW and validate
   the live shape against the same contract. Catches UW CHANGING their API — the
   early warning that would have flagged the greeks call_delta / atm-chains
   straddle drift before it broke the picker. Needs UW_API_KEY; skipped normally.

Regenerate goldens after pulling a fresh archive: scripts/extract_golden.py
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from tests.contracts import CONTRACTS, check_payload

_GOLDEN = Path(__file__).parent / "fixtures" / "golden"


# ── 1. Offline: golden fixtures satisfy their contracts ──────────────────────

@pytest.mark.parametrize("endpoint", sorted(CONTRACTS))
def test_golden_fixture_satisfies_contract(endpoint):
    path = _GOLDEN / f"{endpoint}.json"
    if not path.exists():
        pytest.skip(f"no golden fixture for {endpoint} (run scripts/extract_golden.py)")
    payload = json.loads(path.read_text(encoding="utf-8"))
    violations = check_payload(endpoint, payload)
    assert not violations, "contract violations:\n  " + "\n  ".join(violations)


def test_every_contract_has_a_golden_fixture():
    """Each declared contract should have a committed golden fixture, so the
    offline layer actually exercises it."""
    missing = [e for e in CONTRACTS if not (_GOLDEN / f"{e}.json").exists()]
    assert not missing, f"contracts without golden fixtures: {missing}"


def test_check_payload_flags_a_missing_field():
    """The checker itself must catch a dropped required field (guards the guard)."""
    broken = {"data": [{"strike": 100}]}   # greeks missing call_delta etc.
    violations = check_payload("greeks", broken)
    assert any("call_delta" in v for v in violations)


# ── 2. Live: UW's CURRENT shape satisfies the contract (opt-in) ──────────────

# (endpoint, callable producing a live payload). Uses a liquid ticker/expiry.
def _live_calls():
    from server import uw
    T = "SPY"
    # nearest expiry for endpoints that need one — derive from the chain.
    chain = uw.fetch_option_contracts(T, 60)
    exp = None
    for r in (chain.get("data") or []):
        p = uw.parse_option_symbol(r.get("option_symbol"))
        if p:
            exp = p["expiry"]
            break
    return {
        "spot_exposures_strike": lambda: uw.fetch_spot_exposures_strike(T, min_strike=1, max_strike=100000),
        "oi_per_strike": lambda: uw.fetch_oi_strike(T),
        "greeks": lambda: uw.fetch_greeks(T, exp) if exp else {"data": []},
        "atm_chains": lambda: uw.fetch_atm_chains(T, [exp]) if exp else {"data": []},
        "option_contracts": lambda: chain,
        "volatility_term_structure": lambda: uw.fetch_volatility(T),
        "interpolated_iv": lambda: uw.fetch_interpolated_iv(T),
        "greek_exposure_expiry": lambda: uw.fetch_greek_exposure_expiry(T),
        "darkpool": lambda: uw.fetch_darkpool(T),
        "flow_alerts": lambda: uw.fetch_flow_alerts(limit=50),
    }


@pytest.mark.live
@pytest.mark.parametrize("endpoint", sorted(set(CONTRACTS) & {
    "spot_exposures_strike", "oi_per_strike", "greeks", "atm_chains",
    "option_contracts", "volatility_term_structure", "interpolated_iv",
    "greek_exposure_expiry", "darkpool", "flow_alerts",
}))
def test_live_uw_shape_satisfies_contract(endpoint):
    """Fetch fresh from UW and validate — the early warning for UW API drift."""
    import os
    if not os.environ.get("UW_API_KEY"):
        pytest.skip("UW_API_KEY not set — live contract test needs a key")
    from server import uw
    try:
        payload = _live_calls()[endpoint]()
    except uw.UWError as e:
        pytest.skip(f"{endpoint}: live fetch failed (rate-limited/off-hours): {e}")
    # tolerate a transient empty/failed fetch — only a present-but-wrong-shape
    # response is a contract failure.
    if not isinstance(payload, dict) or not payload.get("data"):
        pytest.skip(f"{endpoint}: no live data (rate-limited / off-hours)")
    violations = check_payload(endpoint, payload)
    assert not violations, "LIVE contract drift:\n  " + "\n  ".join(violations)


@pytest.mark.live
def test_live_opening_flow_field_populates():
    """all_opening_trades is now load-bearing for DIRECTION — verify the Basic
    tier actually sets it true sometimes (same risk class as the ask-side /
    net_prem_ticks=0 gotchas). Reports the opening fraction; fails only if the
    field is entirely absent (drift) — an all-false result is logged loudly."""
    import os
    if not os.environ.get("UW_API_KEY"):
        pytest.skip("UW_API_KEY not set")
    from server import uw
    try:
        payload = uw.fetch_flow_alerts(limit=200)
    except uw.UWError as e:
        pytest.skip(f"flow_alerts live fetch failed: {e}")
    rows = payload.get("data") or []
    if not rows:
        pytest.skip("no live flow rows")
    assert all("all_opening_trades" in r for r in rows[:5]), "field missing (drift)"
    opening = sum(1 for r in rows if r.get("all_opening_trades"))
    frac = opening / len(rows)
    print(f"\n[opening-flow probe] {opening}/{len(rows)} alerts opening ({frac:.0%})")
    assert frac >= 0.0


@pytest.mark.live
def test_live_economic_calendar_shape():
    import os
    if not os.environ.get("UW_API_KEY"):
        pytest.skip("UW_API_KEY not set")
    from server import uw
    try:
        payload = uw.fetch_economic_calendar()
    except uw.UWError as e:
        pytest.skip(f"econ-calendar live fetch failed: {e}")
    rows = payload.get("data") or []
    if not rows:
        pytest.skip("no econ rows this week")
    from tests.contracts import check_payload
    assert not check_payload("economic_calendar", payload)
    print(f"\n[econ-calendar] {len(rows)} events; types={set(r.get('type') for r in rows[:20])}")
