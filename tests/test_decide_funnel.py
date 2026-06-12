"""decide funnel tests (Phase 4 integration) — the LOCKED combination structure from the
decide spec: families + divergence-veto-not-agreement-bonus + skew oppose-veto asymmetry +
cost/structural guards + regime advisory + honest-degrade gated.
"""
from server.models import (Conviction, Cost, DealerGamma, Flow, Positioning, Provenance,
                           Quality, Skew)
from server.pipeline.decide import decide


def _flow(direction="calls", basis="opening_flow"):
    return Flow(direction=direction, direction_basis=basis, call_prem=1e6, put_prem=1e5,
                provenance=Provenance())


def _favorable_inputs(direction="calls"):
    """A signal map that SHOULD resolve Favorable: opening flow, OI building, cost ok,
    NEG gamma (trend), skew agreeing. (Regime is NOT a per-ticker leg.)"""
    return {
        "flow": _flow(direction),
        "positioning": Positioning(confirmation="building", side="call"),
        "conviction": Conviction(direction=direction, dir_delta=100),
        "skew": Skew(rr25=0.05, lean="call_skew"),       # agrees with calls
        "dealer_gamma": DealerGamma(gex_sign="NEG", flip_status="ok"),
        "cost": Cost(guard="ok", ivr=40),
    }


# ── the happy path resolves Favorable ─────────────────────────────────────────
def test_all_aligned_is_favorable():
    v = decide(_favorable_inputs("calls"))
    assert v.overall == "Favorable"
    assert v.direction == "calls"
    assert not v.signal_conflict


# ── agreement NEVER promotes (the anti-double-count rule) ──────────────────────
def test_skew_agreement_is_not_required_and_not_a_peer_green():
    """Removing skew agreement (→ neutral) must NOT change a Favorable to worse: agree was
    never carrying it. And skew agreeing must never UPGRADE a Mixed to Favorable on its own."""
    base = _favorable_inputs("calls")
    base["skew"] = Skew(rr25=0.0, lean="neutral")
    assert decide(base).overall == "Favorable"          # still favorable without skew's agree


def test_conviction_agreement_adds_no_bonus():
    """Same-family conviction agreeing must not promote; a Mixed (cost caution) stays Mixed
    even though conviction agrees with flow."""
    m = _favorable_inputs("calls")
    m["cost"] = Cost(guard="caution", ivr=70)           # knocks Favorable → Mixed
    m["conviction"] = Conviction(direction="calls", dir_delta=999)   # strongly agrees
    assert decide(m).overall == "Mixed"                 # agreement did not rescue it


# ── divergence vetoes (within-family conviction, orthogonal skew) ─────────────
def test_conviction_divergence_caps_to_mixed():
    base = _favorable_inputs("calls")
    base["conviction"] = Conviction(direction="puts", dir_delta=-100)   # diverges
    v = decide(base)
    assert v.overall == "Mixed"
    assert v.signal_conflict and "conviction" in v.conflict_legs


def test_skew_opposition_caps_to_mixed():
    base = _favorable_inputs("calls")
    base["skew"] = Skew(rr25=-0.05, lean="put_skew")    # opposes calls
    v = decide(base)
    assert v.overall == "Mixed"
    assert "skew" in v.conflict_legs


# ── guards: cost block → Stand down; POS gamma caps Favorable ─────────────────
def test_cost_block_is_stand_down():
    base = _favorable_inputs("calls")
    base["cost"] = Cost(guard="block", reason="earnings in 2d")
    assert decide(base).overall == "Stand down"


def test_pos_gamma_caps_below_favorable():
    base = _favorable_inputs("calls")
    base["dealer_gamma"] = DealerGamma(gex_sign="POS", flip_status="ok")   # pinning
    assert decide(base).overall == "Mixed"              # structural yellow → not Favorable


# ── weaker basis caps; positioning owns the side ──────────────────────────────
def test_total_flow_basis_caps_at_yellow_not_favorable():
    base = _favorable_inputs("calls")
    base["flow"] = _flow("calls", basis="total_flow")
    assert decide(base).overall == "Mixed"              # positioning yellow → never Favorable


def test_oi_unwinding_caps_to_mixed():
    base = _favorable_inputs("calls")
    base["positioning"] = Positioning(confirmation="unwinding", side="call")
    assert decide(base).overall == "Mixed"


# ── the side's own bar: weak lean caps at Mixed (reviewer 2026-06-11) ─────────
def test_weak_lean_caps_at_mixed_never_favorable():
    """'Weak evidence, unopposed' must not read Favorable — the base signal has its own
    bar (dominance + floor), and failing it caps exactly like a weaker basis."""
    base = _favorable_inputs("calls")
    base["flow"] = Flow(direction="calls", direction_basis="opening_flow",
                        call_prem=6e5, put_prem=4.5e5, lean_quality="weak",
                        lean_note="1.3:1", provenance=Provenance())
    v = decide(base)
    assert v.overall == "Mixed"
    assert any("flow lean weak (1.3:1)" in r for r in v.reasons)
    assert "weak lean" in v.caps


def test_qualified_lean_still_favorable():
    base = _favorable_inputs("calls")
    base["flow"] = Flow(direction="calls", direction_basis="opening_flow",
                        call_prem=2e6, put_prem=5e5, lean_quality="qualified",
                        lean_note="4:1", provenance=Provenance())
    assert decide(base).overall == "Favorable"


# ── caps: every gate that blocked Favorable, named (the base-rate instrument) ──
def test_caps_empty_on_favorable():
    assert decide(_favorable_inputs("calls")).caps == []


def test_caps_name_every_binding_gate_not_just_the_first():
    base = _favorable_inputs("calls")
    base["dealer_gamma"] = DealerGamma(gex_sign="POS", flip_status="ok")
    base["cost"] = Cost(guard="caution", ivr=70)
    base["conviction"] = Conviction(direction="puts", dir_delta=-100)
    v = decide(base)
    assert set(v.caps) == {"gamma pinned", "cost flags", "tape diverges"}


def test_caps_on_stand_down_include_cost_block():
    base = _favorable_inputs("calls")
    base["cost"] = Cost(guard="block", reason="earnings 2d out")
    assert "cost block" in decide(base).caps


# ── honest-degrade gated ──────────────────────────────────────────────────────
def test_unavailable_flow_is_stand_down_and_named():
    v = decide({"flow": Flow(direction=None, direction_basis="unavailable",
                             provenance=Provenance(quality=Quality.UNAVAILABLE, note="no flow alerts"))})
    assert v.overall == "Stand down"
    assert any("flow n/a" in r for r in v.reasons)


def test_unavailable_noncore_named_but_not_fatal():
    """Skew/conviction unavailable → still resolvable, but coverage is NAMED."""
    base = _favorable_inputs("calls")
    base["skew"] = Skew(lean="unavailable", provenance=Provenance(quality=Quality.UNAVAILABLE))
    base["conviction"] = Conviction(direction=None, provenance=Provenance(quality=Quality.UNAVAILABLE))
    v = decide(base)
    assert any("skew n/a" in r for r in v.reasons)
    assert any("tape n/a" in r for r in v.reasons)
    assert v.overall in ("Favorable", "Mixed")          # not forced to Stand down


def test_skew_dark_caps_favorable_at_mixed():
    """With the orthogonal leg unreadable the divergence check can't run → the rare green
    is not certifiable (same treatment as structural). Neutral skew still permits it."""
    base = _favorable_inputs("calls")
    base["skew"] = Skew(lean="unavailable", provenance=Provenance(quality=Quality.UNAVAILABLE))
    assert decide(base).overall == "Mixed"
    base["skew"] = Skew(rr25=0.0, rr_delta=0.0, lean="neutral")
    assert decide(base).overall == "Favorable"          # readable + no lean = fine


def test_no_same_family_stacking_two_concordant_flow_reads():
    """opening-$ flow bullish AND greek-flow conviction bullish must NOT beat flow alone:
    both Favorable, identical overall — agreement added no promotion."""
    one = _favorable_inputs("calls")
    one.pop("conviction")                               # flow alone (conviction absent)
    two = _favorable_inputs("calls")                    # flow + agreeing conviction
    assert decide(one).overall == decide(two).overall == "Favorable"


# ── audit: signals_used lists exactly what was consumed ───────────────────────
def test_signals_used_lists_consumed_signals():
    v = decide(_favorable_inputs("calls"))
    assert set(v.signals_used) == {"flow", "positioning", "conviction", "skew",
                                   "dealer_gamma", "cost"}


def test_direction_comes_from_flow_not_a_guard():
    """The verdict's direction comes from flow/positioning, never a guard signal."""
    v = decide(_favorable_inputs("puts"))
    assert v.direction == "puts"
