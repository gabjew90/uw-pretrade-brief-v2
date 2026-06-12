"""Present snapshot tests — directive §5.4, ported from the prototype's
js/uw-contract-tests.js. These assert over the PRESENT-stage view model
(the contract), not pixels: if present.py can emit it, the dumb frontend
will render it, so the budget is enforceable here.

Wire `vm_for(ticker)` to the real present() output over the bronze replay
fixtures (REPLAY=1), plus the synthetic PERFECT/veto fixtures from
tests/fixtures once they exist. Drop-in target: tests/test_present_snapshot.py
"""
import json
import re

import pytest

# ---------------------------------------------------------------- helpers

BANNED = re.compile(r"\b(Mixed|Favorable)\b")
VERDICT_VOCAB = {"PERFECT", "NOT NOW"}
GATE_NAMES = {"smart_flow", "dealer_fuel", "cheap_vol", "good_entry",
              "no_squeeze", "cheap_event"}


def walk_strings(obj):
    """Yield every string anywhere in a view-model payload."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from walk_strings(v)


def direction_vms(vm):
    """Both per-direction verdict blocks of a ticker ViewModel."""
    return [d for d in (vm.get("calls"), vm.get("puts")) if d]


# ---------------------------------------------------------------- §5.4 tests

def test_banned_words_never_render(all_viewmodels):
    """'Mixed' and 'Favorable' are banned strings — fail if they appear
    ANYWHERE in any emitted view model, captions and subtext included."""
    for vm in all_viewmodels:
        for s in walk_strings(vm):
            assert not BANNED.search(s), f"banned word in: {s!r}"


def test_verdict_vocabulary(all_viewmodels):
    for vm in all_viewmodels:
        for d in direction_vms(vm):
            assert d["state"] in VERDICT_VOCAB
            if d["state"] == "NOT NOW":
                assert 0 <= d["green"] < d["total"]
                assert d["waiting"], "NOT NOW must carry a waiting line"
            else:
                assert d["green"] == d["total"]
                assert d.get("waiting") is None


def test_default_render_budget(all_viewmodels):
    """≤5 gate rows; exactly one chart (the smart_flow strip); ≤4 numbers;
    numbers only at PERFECT or n ≥ N−1."""
    for vm in all_viewmodels:
        for d in direction_vms(vm):
            gates = d["gates"]
            assert len(gates) <= 5
            strips = [g for g in gates if g.get("flow")]
            assert all(g["name"] == "smart_flow" for g in strips)
            assert len(strips) <= 1, "the flow strip is the ONLY default chart"
            nums = d.get("numbers")
            if nums is not None:
                assert len(nums) <= 4
                assert d["green"] >= d["total"] - 1, \
                    "numbers only at PERFECT or one gate short"


def test_no_provenance_or_logic_on_default(all_viewmodels):
    """Provenance/as_of/logic strings live ONLY in why.subtext (the expanded
    panel). Nothing on the gate row or card surface may carry them."""
    for vm in all_viewmodels:
        for d in direction_vms(vm):
            for g in d["gates"]:
                surface = {k: v for k, v in g.items() if k != "why"}
                for s in walk_strings(surface):
                    assert "as of" not in s and "archive" not in s \
                        and "derived" not in s, \
                        f"provenance leaked to default render: {s!r}"
                assert "logic" not in g, "logic lines are deleted"


def test_why_panel_micro_visuals(all_viewmodels):
    """One micro-visual per gate; none for no_squeeze (checklist only);
    DARK chart-gates carry missing[] and no fabricated data."""
    for vm in all_viewmodels:
        for d in direction_vms(vm):
            for g in d["gates"]:
                why = g.get("why") or {}
                if g["name"] == "no_squeeze":
                    assert "kind" not in why or why["kind"] is None
                    assert why.get("items"), "no_squeeze renders named checks"
                elif g["state"] == "dark":
                    assert why.get("missing"), "DARK must name absent inputs"
                    assert not why.get("data"), "DARK never fabricates a chart"
                else:
                    assert why.get("kind") in {"tug", "ladder", "cheap_vol",
                                               "runway", "dot_strip"}
                    assert why.get("data"), "value-anchored visuals need data"


def test_dark_counts_against_verdict(all_viewmodels):
    for vm in all_viewmodels:
        for d in direction_vms(vm):
            greens = sum(1 for g in d["gates"] if g["state"] == "green")
            assert d["green"] == greens, "DARK and RED both count not-green"


def test_no_squeeze_veto_named_first(all_viewmodels):
    """When no_squeeze is RED on puts, the waiting line names it first."""
    for vm in all_viewmodels:
        d = vm.get("puts")
        if not d:
            continue
        ns = next((g for g in d["gates"] if g["name"] == "no_squeeze"), None)
        if ns and ns["state"] == "red":
            first = d["waiting"].removeprefix("Waiting on: ").split(",")[0]
            assert first.strip() == ns["short"], \
                "hard veto must lead the waiting line"


def test_catalyst_branch_shape(all_viewmodels):
    """Catalyst: cheap_event present, clean-window gate (cheap_vol) dropped,
    tag rendered; drift: no cheap_event, no tag."""
    for vm in all_viewmodels:
        for d in direction_vms(vm):
            names = {g["name"] for g in d["gates"]}
            assert names <= GATE_NAMES
            if d["branch"] == "catalyst":
                assert "cheap_event" in names and "cheap_vol" not in names
                assert d.get("tag")
            else:
                assert "cheap_event" not in names and d.get("tag") is None


# ---------------------------------------------------------------- fixture

def _contract_dump(vm) -> dict:
    """The slice of the ViewModel the v3 frontend consumes (Present Contract
    Extensions §5): {ticker, best, calls, puts}."""
    return {"ticker": vm.ticker, "best": vm.best, "calls": vm.calls, "puts": vm.puts}


def _synthetic_cases():
    """One signal map per verdict state the prototype fixtures cover: PERFECT,
    NOT NOW with one RED, catalyst branch with tag, a DARK gate, and a no_squeeze
    RED veto on puts."""
    from server.models import Catalyst, Provenance, Quality, Shorts, Vol
    from server.pipeline.decide import decide
    from server.pipeline.present import present
    from tests.test_decide_funnel import _sigs

    cases = []

    perfect = _sigs("calls")
    cases.append(("NVDA", perfect))

    one_red = _sigs("calls")
    one_red["vol"] = Vol(ivr=85, hv=0.22, iv_front=0.20, hv_iv_ratio=1.1,
                         term_slope=0.01, iv_spike_pct=2.0)
    cases.append(("AMD", one_red))

    catalyst = _sigs("puts")
    catalyst["catalyst"] = Catalyst(days_to_earnings=2, report_date="2026-06-18",
                                    implied_move_pct=6.1, hist_move_pct=8.9,
                                    moves=[7.2, 11.4, 5.9, 13.1], quarters=4, ratio=0.69)
    cases.append(("ORCL", catalyst))

    dark = _sigs("calls")
    dark["vol"] = Vol(provenance=Provenance(quality=Quality.UNAVAILABLE,
                                            note="no volatility inputs"))
    cases.append(("MSFT", dark))

    veto = _sigs("puts")
    veto["shorts"] = Shorts(ftd_latest=99999, ftd_pctile=99.0)
    cases.append(("GME", veto))

    return [_contract_dump(present(t, s, decide(s))) for t, s in cases]


@pytest.fixture(scope="module")
def all_viewmodels(tmp_path_factory):
    """present() over the REPLAY bronze archive (the golden SPY session) plus the
    synthetic gate fixtures — every verdict state the prototype renders."""
    from server.services import storage
    from tests.replay_harness import build_replay_vm, seed_lake

    tmp = tmp_path_factory.mktemp("lake")
    roots = {"bronze": tmp / "bronze", "silver": tmp / "silver", "gold": tmp / "gold"}
    orig = storage._tier_root
    storage._tier_root = lambda tier: roots[tier]
    try:
        seed_lake(roots)
        vms = [_contract_dump(build_replay_vm())]
    finally:
        storage._tier_root = orig
    vms += _synthetic_cases()
    return vms
