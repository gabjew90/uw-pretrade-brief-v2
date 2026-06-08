"""Hyphenated-path CI lint (ops-ci spec §1; port of e1d6c5e:tests/test_uw_paths.py).

UW uses HYPHENATED routes everywhere (flow-alerts, spot-exposures,
historical-risk-reversal-skew). An underscore in a path segment 404s — and a 404
degrades silently to 'unavailable' downstream, so the bug is invisible.
`historical_risk_reversal_skew` shipped that way in v2 and failed for weeks.

v3 difference: paths are not centralised in one `uw.py`; callers pass them to
`uw_client.get()`. So this lint scans ALL of `server/` for REST-path-like string
literals and asserts hyphenation. It passes cleanly today (no UW paths hardcoded yet —
they arrive in Phase 3) and activates automatically as endpoints land. The runtime
`uw_client.assert_hyphenated()` guard is the second layer; this is the static backstop.
"""
import re
from pathlib import Path

_SERVER = Path(__file__).parent.parent / "server"

# A REST-ish path literal: starts with "/", lowercase, at least two segments, made of
# url-safe segment chars (letters/digits/hyphen/underscore) and {placeholders}. This
# matches both a correct "/option-trades/flow-alerts" and a BUGGY "/x/flow_alerts"
# (the underscore form has no hyphen, so we must NOT filter on hyphen presence).
_PATH_RE = re.compile(r"""["'](/[a-z0-9][a-z0-9\-_{}]*(?:/[a-z0-9\-_{}]+)+)["']""")


def _path_literals() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for py in _SERVER.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        for m in _PATH_RE.finditer(src):
            found.append((py, m.group(1)))
    return found


def _segments_have_underscore(path: str) -> bool:
    # strip {placeholder} interpolations, then check each resource segment
    body = re.sub(r"\{[^}]*\}", "", path)
    return any("_" in seg for seg in body.strip("/").split("/") if seg)


def test_uw_api_paths_use_hyphens_not_underscores():
    offenders = [(str(p.relative_to(_SERVER.parent)), lit)
                 for p, lit in _path_literals() if _segments_have_underscore(lit)]
    assert not offenders, (
        "REST path literals must be hyphenated, not underscored (UW underscore → 404 → "
        f"silent degrade). Offenders: {offenders}")
