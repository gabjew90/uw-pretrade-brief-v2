"""The one rule, enforced at CI (ops-ci spec §3): the frontend computes NOTHING.

Greps the served frontend (static/index.html + static/js/) for signal/threshold/clock
logic. The client may map server-supplied fields to pixels (geometry is its ONLY math,
per the Present contract) but may never derive a value, compare a gate threshold, or
consult the clock to make a decision.

Exemptions: uw-fixtures.js (demo DATA — server-shaped example strings, not logic) and
uw-contract-tests.js (the in-page checker — it necessarily CONTAINS the banned-word
regex it enforces).
"""
import re
from pathlib import Path

_STATIC = Path(__file__).parent.parent / "static"
_EXEMPT = {"uw-fixtures.js", "uw-contract-tests.js"}


def _sources() -> dict[str, str]:
    out = {"index.html": (_STATIC / "index.html").read_text(encoding="utf-8")}
    for f in sorted((_STATIC / "js").glob("*")):
        if f.name not in _EXEMPT:
            out[f.name] = f.read_text(encoding="utf-8")
    return out


BANNED = [
    (r"\bvolume_oi_ratio\b", "opening-flow proxy — direction math is server-only"),
    (r"\bgex_sign\b|\bcall_gamma\b|\bput_gamma\b", "gamma sign/ladder math — server-only"),
    (r"\biv_rank\b|\bivr\b", "cost gate input — server-only"),
    (r"\bnew Date\(", "clock-for-decisions — session/staleness comes from provenance"),
    (r"\brisk_reversal\b|\brr25\b", "skew math — server-only"),
    (r"\bbreakeven\b.*[*/+-]|[*/+-].*\bbreakeven\b", "breakeven arithmetic — server-only"),
    (r"\btotal_premium\b\s*[*/+<>-]", "premium arithmetic — server-only"),
    (r"if\s*\(.*direction\s*===", "decision branch on the direction value"),
]


def _strip(txt: str) -> str:
    """Drop comments and SVG presentation attrs before pattern-matching (an
    opacity=\"0.35\" is paint, not a delta-band constant)."""
    txt = re.sub(r"//[^\n]*", "", txt)
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    return re.sub(r"opacity=\"[^\"]*\"|opacity=\{[^}]*\}", "", txt)


def test_frontend_computes_nothing():
    offenders = []
    for name, src in _sources().items():
        offenders += [(name, pat, why) for pat, why in BANNED
                      if re.search(pat, _strip(src))]
    assert not offenders, (
        "client-side computation detected — the frontend renders the "
        f"ViewModel verbatim, it never derives: {offenders}")


def test_frontend_has_no_threshold_constants():
    """No magic gate numbers in JS (0.35/0.55 delta band, IV-rank 30 bar, 15% spread).
    Constants that feed visuals arrive IN the view model (rankPassMax, buildFrac,
    threshPct, passFrac) — the client never hardcodes them."""
    for name, src in _sources().items():
        js = _strip(src).replace("g.threshold", "")
        for needle in ("0.35", "0.55", "_THR", "threshold =", "15.0"):
            assert needle not in js, f"threshold-like constant {needle!r} in {name}"


def test_frontend_has_no_banned_verdict_words():
    """Directive acceptance #5: 'Mixed' and 'Favorable' never regress into the client."""
    for name, src in _sources().items():
        assert "Mixed" not in src and "Favorable" not in src, name
