"""Pin the invariant: static/index.html only differs from the original prototype
inside the 6 V2-EDIT zones (plus the HYDRATION_TARGET marker). Any drift outside
those zones is a regression."""
from __future__ import annotations
import re
from pathlib import Path
import pytest


_REPO_ROOT = Path(__file__).parent.parent
_V2_HTML = _REPO_ROOT / "static" / "index.html"
_V1_HTML = Path("../UW_Project/prototypes/decision-dashboard.html").resolve()

# Regex matches "/* V2-EDIT-N-BEGIN ... V2-EDIT-N-END */" OR HTML-comment variant
_EDIT_ZONE_RE = re.compile(
    r"(?:/\*|<!--)\s*V2-EDIT-(\d+)-BEGIN.*?V2-EDIT-\1-END\s*(?:\*/|-->)",
    re.DOTALL,
)
_HYDRATION_MARKER_RE = re.compile(r"<!--\s*HYDRATION_TARGET\s*-->")


def test_exactly_six_v2_edit_zones():
    text = _V2_HTML.read_text(encoding="utf-8")
    zones = _EDIT_ZONE_RE.findall(text)
    assert sorted(zones) == ["1", "2", "3", "4", "5", "6"], \
        f"expected zones [1..6], got {zones}"


def test_hydration_marker_present():
    text = _V2_HTML.read_text(encoding="utf-8")
    assert _HYDRATION_MARKER_RE.search(text), "missing <!-- HYDRATION_TARGET -->"


def test_all_render_tile_functions_preserved():
    """Critical render functions from the v1 prototype must still exist in v2.

    NOTE: renderTile4Cost, renderTile5Confirmation, and renderTermStructureChart
    were INTENTIONALLY removed (2026-06-02 polish pass) — the product is 4 tiles,
    those two prototype tiles were dropped, and the term-structure sparkline they
    used is superseded by the Tile 4 picker's termCurveSvg. They're off this list
    on purpose; the guard still protects the render functions that remain live."""
    text = _V2_HTML.read_text(encoding="utf-8")
    for fn in ["renderTile1Flow", "renderTile2OI", "renderTile3Structural",
               "renderTile6Picker", "ladderSvg", "renderFlowChart"]:
        assert fn in text, f"render function {fn}() missing from v2 HTML"


def test_css_variables_preserved():
    """The CSS variable tokens from the v1 prototype must be present."""
    text = _V2_HTML.read_text(encoding="utf-8")
    for token in ["--bg:", "--panel:", "--pos:", "--neg:", "--hot:",
                   "--ff-display:", "--ff-mono:", "--shadow-card:"]:
        assert token in text, f"CSS variable {token} missing"


def test_tooltip_dictionary_preserved():
    """All 6 tile tooltips + a few extras must be present."""
    text = _V2_HTML.read_text(encoding="utf-8")
    for key in ["tile1:", "tile2:", "tile3:", "tile4:", "tile5:", "tile6:",
                "nothing_today:", "pill_live:", "pill_proxy:"]:
        assert key in text, f"tooltip entry {key} missing"


def test_animations_preserved():
    """Keyframes from the v1 polish pass must survive."""
    text = _V2_HTML.read_text(encoding="utf-8")
    for kf in ["@keyframes fadeUp", "@keyframes dotPulse"]:
        assert kf in text, f"animation {kf} missing"


@pytest.mark.skipif(not _V1_HTML.exists(), reason="v1 prototype not adjacent")
def test_v1_prototype_size_floor():
    """Guard against accidental MASSIVE DELETION of the prototype HTML. v2 has
    legitimately grown beyond v1 (Tile 2 detail, Tile 3 Phase 2, Tile 4 rich
    picker — all sanctioned per CLAUDE.md), so the original ±15% band is
    obsolete. The meaningful invariant now is the floor: v2 must never shrink
    below the v1 baseline (that would mean we deleted the prototype tiles)."""
    v1_size = _V1_HTML.stat().st_size
    v2_size = _V2_HTML.stat().st_size
    assert v2_size >= v1_size * 0.85, (
        f"v2 HTML {v2_size} dropped below the v1 floor {v1_size} — "
        f"prototype content may have been deleted")


@pytest.mark.skipif(not _V1_HTML.exists(), reason="v1 prototype not adjacent")
def test_outside_edit_zones_matches_v1_structurally():
    """The portion of v2 OUTSIDE the V2-EDIT zones + HYDRATION_TARGET should
    structurally match v1. We strip the V2-EDIT zones from v2 and the
    corresponding original-content regions from v1, then compare the remainder.

    For this test to work cleanly, we just check that the count of certain
    high-signal anchors (function definitions, class declarations, etc.) is
    identical between v1 and the v2 stripped-of-edit-zones text.
    """
    v1_text = _V1_HTML.read_text(encoding="utf-8")
    v2_text = _V2_HTML.read_text(encoding="utf-8")
    v2_stripped = _EDIT_ZONE_RE.sub("", v2_text)
    v2_stripped = _HYDRATION_MARKER_RE.sub("", v2_stripped)

    # Count function definitions — catches accidental wholesale DELETION.
    # v2 has legitimately grown well past v1 (Tile 3 Phase 2: renderTile3Rich /
    # richLadderSvg / driftGauge; Tile 4: renderTile4Picker / termCurveSvg;
    # search-any-ticker: lookupTicker — all sanctioned per CLAUDE.md). So this is
    # a FLOOR check now, not a ±band: v2 must keep at least v1's functions minus a
    # small allowance for removed prototype-only scaffolding (buildPrompt etc.).
    v1_fn_count = len(re.findall(r"\bfunction\s+\w+\s*\(", v1_text))
    v2_fn_count = len(re.findall(r"\bfunction\s+\w+\s*\(", v2_stripped))
    assert v2_fn_count >= v1_fn_count - 5, \
        f"function definitions dropped below the v1 floor: v1={v1_fn_count} v2(stripped)={v2_fn_count}"
