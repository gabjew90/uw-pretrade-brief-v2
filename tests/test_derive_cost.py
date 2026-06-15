"""derive_cost tests (Phase 4) — the cost GUARD now includes the load-bearing spread-cost
+ expected-move-vs-breakeven gate. Golden uses the real interpolated-iv + option-contracts.

Block precedence: earnings/event → spread too wide → priced-move-can't-reach-breakeven →
(else IV bands). Without a chain the spread gate is un-evaluable → caution, NEVER a
confident green (the edge can't be confirmed to clear the cost).
"""
import json
from pathlib import Path

from server.models import IVTermPoint, OptionContract, Quality, TermStructurePoint
from server.pipeline.derive import _pick_contract, derive_cost
from server.pipeline.ingest import RawRecord
from server.pipeline.normalize import normalize

IV_FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "interpolated-iv" / "SPY.json"
OC_FIXTURE = Path(__file__).parent / "fixtures" / "bronze" / "option-contracts" / "SPY.json"
ASOF = "2026-06-08"


def _iv(days, percentile, move=0.02):
    return IVTermPoint(date="2026-06-08", days=days, percentile=percentile, implied_move_perc=move)


def _term(*pairs, move=0.05):
    return [_iv(d, p, move) for d, p in pairs]


def _contract(side="call", strike=100.0, bid=1.00, ask=1.05, expiry="2026-06-12"):
    return [OptionContract(type=side, strike=strike, expiry=expiry, bid=bid, ask=ask)]


def _tradeable(*, ivr_pct=0.40, side="call", strike=100.0, bid=1.00, ask=1.05,
               spot=100.0, move=0.05):
    """A canon where the spread gate IS evaluable (chain + spot + side present)."""
    return {"iv_term": _term((30, ivr_pct), move=move), "option_contracts":
            _contract(side, strike, bid, ask), "flow_side": side, "spot": spot}


# ── both directions priced (so good_entry can show the call AND the put) ──────
def test_prices_both_directions_not_just_flow_side():
    """The chain has calls and puts; cost.contracts carries a pick for each side, so the
    non-flow direction's good_entry isn't a dead 'no contract priced'."""
    canon = {"iv_term": _term((30, 0.40)), "flow_side": "put", "spot": 100.0,
             "option_contracts": [
                 OptionContract(type="call", strike=100.0, expiry="2026-06-12", bid=1.0, ask=1.05),
                 OptionContract(type="put", strike=100.0, expiry="2026-06-12", bid=1.0, ask=1.05)]}
    c = derive_cost(canon, asof=ASOF)
    assert set(c.contracts) == {"call", "put"}
    assert c.contracts["call"]["type"] == "call" and c.contracts["put"]["type"] == "put"
    assert c.contract == c.contracts["put"]        # flow side stays the primary contract


# ── IV-rank bands (chain present so gate is evaluable) ────────────────────────
def test_low_ivr_with_tradeable_chain_is_ok():
    c = derive_cost(_tradeable(ivr_pct=0.40), asof=ASOF)
    assert c.guard == "ok"
    assert c.ivr == 40.0
    assert c.spread_pct is not None


def test_mid_ivr_is_caution():
    assert derive_cost(_tradeable(ivr_pct=0.70), asof=ASOF).guard == "caution"


def test_high_ivr_is_caution_not_block():
    # rich IV is a DEGRADER, not an EV-killer — a right move can win through it → caution
    c = derive_cost(_tradeable(ivr_pct=0.95), asof=ASOF)
    assert c.guard == "caution"
    assert "rich" in c.reason


# ── the load-bearing spread gate (EV-killer → block) ──────────────────────────
def test_absurd_spread_blocks_regardless():
    # bid 1.00 / ask 1.40 → ~33% of mid → dead regardless of move (>= hard cap)
    c = derive_cost(_tradeable(ivr_pct=0.20, bid=1.00, ask=1.40, move=0.30), asof=ASOF)
    assert c.guard == "block"
    assert "spread" in c.reason


def test_modest_spread_vs_tiny_move_blocks_on_burden():
    # ~12% spread against a 0.5% priced move → friction ≫ the move → dead
    c = derive_cost(_tradeable(ivr_pct=0.20, bid=1.00, ask=1.13, move=0.005), asof=ASOF)
    assert c.guard == "block"
    assert c.spread_pct > 10 and c.expected_move_pct < 1


def test_wide_spread_against_big_move_is_not_block():
    # 15% spread against a 30% priced move → fine/borderline, NOT dead (operator's example)
    c = derive_cost(_tradeable(ivr_pct=0.20, bid=1.00, ask=1.16, move=0.30), asof=ASOF)
    assert c.guard != "block"                      # a big move clears a moderate spread


def test_liquid_tight_spread_small_move_not_blocked():
    # SPY-like: ~4% spread, 0.6% move → tight spread should NOT hard-block (only caution)
    c = derive_cost(_tradeable(ivr_pct=0.20, bid=1.00, ask=1.04, move=0.006), asof=ASOF)
    assert c.guard == "caution"                    # degraded, not killed


def test_tight_spread_clean_move_is_ok():
    c = derive_cost(_tradeable(ivr_pct=0.20, bid=1.00, ask=1.02, move=0.08), asof=ASOF)  # ~2%, 8% move
    assert c.guard == "ok"


# ── expected-move-vs-breakeven is a DEGRADER (caution), not a kill ────────────
def test_priced_move_below_breakeven_is_caution_not_block():
    # priced move tight vs breakeven → caution: a correct LARGER move can still win through
    c = derive_cost(_tradeable(ivr_pct=0.20, strike=100.0, bid=1.00, ask=1.04,
                               spot=100.0, move=0.005), asof=ASOF)
    assert c.guard == "caution"
    assert "breakeven" in c.reason
    assert c.expected_move_pct < c.breakeven_move_pct


def test_priced_move_clears_breakeven_ok():
    c = derive_cost(_tradeable(ivr_pct=0.20, move=0.08), asof=ASOF)
    assert c.guard == "ok"
    assert c.expected_move_pct >= c.breakeven_move_pct


# ── event/earnings precedence + data-even-when-blocked ────────────────────────
def test_earnings_blocks_and_still_reports_spread_data():
    canon = _tradeable(ivr_pct=0.20)
    canon["days_to_earnings"] = 3
    c = derive_cost(canon, asof=ASOF)
    assert c.guard == "block" and "earnings" in c.reason
    assert c.spread_pct is not None and c.ivr is not None     # data shown even when blocked


def test_macro_event_cautions_with_name_never_blocks():
    """Operator policy 2026-06-11: macro prints are weekly-cadence, so a hard block would
    PASS most weeks by construction. Inform (name + days) and let the human plan the exit."""
    canon = _tradeable(ivr_pct=0.20)
    canon["event_within_hold"] = True
    canon["macro_event"] = "CPI 2d"
    c = derive_cost(canon, asof=ASOF)
    assert c.guard == "caution"
    assert "macro CPI 2d" in c.reason
    # without the name line the flag still appears, generically
    canon.pop("macro_event")
    assert "macro event in 5d" in derive_cost(canon, asof=ASOF).reason


# ── failed calendars are unknown, never an all-clear (review SEVERE #2) ───────
def test_failed_macro_calendar_caps_ok_to_caution():
    canon = _tradeable(ivr_pct=0.20)
    canon["event_calendar_ok"] = False              # fetch FAILED (vs absent key = not run)
    c = derive_cost(canon, asof=ASOF)
    assert c.guard == "caution"
    assert "macro calendar n/a" in c.reason


def test_failed_earnings_calendar_caps_ok_to_caution():
    canon = _tradeable(ivr_pct=0.20)
    canon["earnings_calendar_ok"] = False
    c = derive_cost(canon, asof=ASOF)
    assert c.guard == "caution"
    assert "earnings dates n/a" in c.reason


def test_null_iv_percentile_is_na_not_zero():
    """A vendor-null percentile must read 'IV rank n/a', never 0/100 ('cheapest ever')."""
    canon = _tradeable(ivr_pct=0.20)
    canon["iv_term"] = [IVTermPoint(date=ASOF, days=30, percentile=None,
                                    implied_move_perc=0.05)]
    c = derive_cost(canon, asof=ASOF)
    assert c.ivr is None
    assert "IV rank n/a" in c.reason
    assert c.guard == "caution"


# ── honest-degrade: no chain → caution, never a confident green ───────────────
def test_no_chain_is_caution_not_ok():
    c = derive_cost({"iv_term": _term((30, 0.20))}, asof=ASOF)   # cheap IV but no chain
    assert c.guard == "caution"                                 # cannot confirm tradeability
    assert "no chain" in c.reason


def test_cost_is_never_a_direction():
    c = derive_cost(_tradeable(), asof=ASOF)
    assert not hasattr(c, "direction")


# ── term-structure overpay (secondary, caution-level) ────────────────────────
def _termstruct(*pairs_dte_iv):
    return [TermStructurePoint(dte=d, volatility=v) for d, v in pairs_dte_iv]


def test_inverted_term_structure_caps_ok_to_caution():
    canon = _tradeable(ivr_pct=0.20)               # would be ok
    canon["term_structure"] = _termstruct((5, 0.30), (30, 0.20))   # front >> back → inverted
    c = derive_cost(canon, asof=ASOF)
    assert c.guard == "caution"
    assert c.term_inverted is True
    assert "inverted" in c.reason


def test_normal_term_structure_leaves_ok():
    canon = _tradeable(ivr_pct=0.20)
    canon["term_structure"] = _termstruct((5, 0.18), (30, 0.20))   # front <= back → normal
    c = derive_cost(canon, asof=ASOF)
    assert c.guard == "ok"
    assert c.term_inverted is False


def test_overpay_never_overrides_a_block():
    canon = _tradeable(ivr_pct=0.20)
    canon["days_to_earnings"] = 2                  # hard block (earnings, not macro)
    canon["term_structure"] = _termstruct((5, 0.40), (30, 0.20))   # inverted
    assert derive_cost(canon, asof=ASOF).guard == "block"    # earnings block wins


# ── weekly-DTE floor on the contract pick ─────────────────────────────────────
def test_pick_skips_0dte_when_a_weekly_exists():
    from datetime import date
    asof_d = date(2026, 6, 8)
    contracts = [
        OptionContract(type="call", strike=100, expiry="2026-06-08", bid=1, ask=1.1),  # 0 DTE
        OptionContract(type="call", strike=100, expiry="2026-06-12", bid=1, ask=1.1),  # 4 DTE weekly
    ]
    pick = _pick_contract(contracts, "call", 100.0, asof_d)
    assert pick.expiry == "2026-06-12"             # skipped the 0-DTE for the real weekly


def test_pick_falls_back_to_0dte_if_no_weekly():
    from datetime import date
    asof_d = date(2026, 6, 8)
    contracts = [OptionContract(type="call", strike=100, expiry="2026-06-08", bid=1, ask=1.1)]
    pick = _pick_contract(contracts, "call", 100.0, asof_d)
    assert pick.expiry == "2026-06-08"             # only a 0-DTE exists → use it


# ── contract guidance: delta band + theta drag + candidates (list item 3) ─────
def _greeks(strike=100.0, call_delta=0.45, put_delta=-0.45, call_theta=-0.05, put_theta=-0.05):
    from server.models import GreeksRow
    return [GreeksRow(strike=strike, call_delta=call_delta, put_delta=put_delta,
                      call_theta=call_theta, put_theta=put_theta)]


def test_pick_carries_delta_and_theta_drag():
    canon = _tradeable(ivr_pct=0.20)
    canon["greeks"] = _greeks(call_delta=0.45, call_theta=-0.05)   # ask 1.05 → ~4.8%/day
    c = derive_cost(canon, asof=ASOF)
    assert c.contract["delta"] == 0.45
    assert c.contract["theta_day_pct"] == 4.8
    assert c.guard == "ok"                                         # in-band, mild theta


def test_low_delta_flags_lottery():
    canon = _tradeable(ivr_pct=0.20)
    canon["greeks"] = _greeks(call_delta=0.15)
    c = derive_cost(canon, asof=ASOF)
    assert c.guard == "caution"
    assert "lottery" in c.reason


def test_high_delta_flags_intrinsic():
    canon = _tradeable(ivr_pct=0.20)
    canon["greeks"] = _greeks(call_delta=0.80)
    c = derive_cost(canon, asof=ASOF)
    assert c.guard == "caution"
    assert "intrinsic" in c.reason


def test_fast_theta_flags():
    canon = _tradeable(ivr_pct=0.20)
    canon["greeks"] = _greeks(call_theta=-0.30)    # 0.30/1.05 ≈ 29%/day
    c = derive_cost(canon, asof=ASOF)
    assert c.guard == "caution"
    assert "theta" in c.reason


def test_put_side_uses_put_leg_abs_delta():
    canon = _tradeable(ivr_pct=0.20, side="put")
    canon["greeks"] = _greeks(put_delta=-0.45, put_theta=-0.05)
    c = derive_cost(canon, asof=ASOF)
    assert c.contract["delta"] == 0.45             # abs of the put leg


def test_candidates_are_same_expiry_neighbours_with_metrics():
    canon = _tradeable(ivr_pct=0.20, strike=100.0)
    canon["option_contracts"] = (
        _contract("call", 100.0) + _contract("call", 101.0) + _contract("call", 99.0)
        + _contract("call", 110.0) + _contract("put", 100.5))      # put excluded
    c = derive_cost(canon, asof=ASOF)
    assert c.contract["strike"] == 100.0
    strikes = [x["strike"] for x in c.candidates]
    assert strikes[:2] == [101.0, 99.0] or strikes[:2] == [99.0, 101.0]  # nearest-money first
    assert 100.5 not in strikes
    assert all("spread_pct" in x and "breakeven_move_pct" in x for x in c.candidates)


def test_missing_greeks_is_not_a_flag():
    """No greeks sheet → delta/theta None, no lottery/theta flag (honest absence)."""
    c = derive_cost(_tradeable(ivr_pct=0.20), asof=ASOF)
    assert c.contract["delta"] is None
    assert c.guard == "ok"


# ── golden: real interpolated-iv + option-contracts ───────────────────────────
def test_golden_real_chain_and_iv():
    iv = normalize(RawRecord(endpoint="/stock/SPY/interpolated-iv", params={}, ticker="SPY",
                             fetched_at="t", content_hash="h",
                             payload=json.loads(IV_FIXTURE.read_text(encoding="utf-8"))))
    chain = normalize(RawRecord(endpoint="/stock/SPY/option-contracts", params={}, ticker="SPY",
                                fetched_at="t", content_hash="h",
                                payload=json.loads(OC_FIXTURE.read_text(encoding="utf-8"))))
    assert len(chain) > 100
    c = derive_cost({"iv_term": iv, "option_contracts": chain, "flow_side": "put",
                     "spot": 744.94}, asof=ASOF)
    assert c.guard in ("ok", "caution", "block")
    assert c.contract is not None                  # a real contract was picked + evaluated
    assert c.spread_pct is not None and c.breakeven_move_pct is not None
