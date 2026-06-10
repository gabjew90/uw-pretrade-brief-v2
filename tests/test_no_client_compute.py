"""The one rule, enforced at CI (ops-ci spec §3): the frontend computes NOTHING.

Greps static/index.html for signal/threshold/clock logic. The client may format and map
server-supplied fields (tone → CSS class, quality → tint) but may never derive a value,
compare a threshold, or consult the clock to make a decision. Extend BANNED as new
signals land (each pattern notes what it guards).
"""
import re
from pathlib import Path

SRC = (Path(__file__).parent.parent / "static" / "index.html").read_text(encoding="utf-8")

BANNED = [
    (r"\bvolume_oi_ratio\b", "opening-flow proxy — direction math is server-only"),
    (r"\bgex_sign\b|\bcall_gamma\b|\bput_gamma\b", "gamma sign/ladder math — server-only"),
    (r"\biv_rank\b|\bivr\b", "cost gate input — server-only"),
    (r"\bnew Date\(", "clock-for-decisions — session/staleness comes from provenance"),
    (r"\brisk_reversal\b|\brr25\b", "skew math — server-only"),
    (r"\bbreakeven\b.*[*/+-]|[*/+-].*\bbreakeven\b", "breakeven arithmetic — server-only"),
    (r"\btotal_premium\b\s*[*/+<>-]", "premium arithmetic — server-only"),
    (r"if\s*\(.*direction\s*===", "decision branch on the direction value"),
    (r"\.toFixed\(\d\)\s*\+\s*['\"]%", "client-side percent derivation — values arrive formatted"),
]


def test_frontend_computes_nothing():
    offenders = [(pat, why) for pat, why in BANNED if re.search(pat, SRC)]
    assert not offenders, (
        "client-side computation detected in static/index.html — the frontend renders the "
        f"ViewModel verbatim, it never derives: {offenders}")


def test_frontend_has_no_threshold_constants():
    """No magic gate numbers in JS (0.35/0.55 delta band, 15% spread, etc.). The only
    numerics allowed are layout/CSS and slice indices. Comments are exempt (they may
    legitimately SAY the word 'threshold')."""
    js = re.sub(r"//.*", "", SRC.split("<script>", 1)[1])
    for needle in ("0.35", "0.55", "_THR", "threshold", "15.0"):
        assert needle not in js, f"threshold-like constant {needle!r} in frontend JS"
