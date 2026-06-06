"""Guard against the path-format bug class: UW uses HYPHENATED routes everywhere
(flow-alerts, spot-exposures, historical-risk-reversal-skew, …). An underscore in
a path segment 404s — and 404s degrade silently to 'unavailable' downstream, so the
bug is invisible. `historical_risk_reversal_skew` shipped this way and failed for
weeks. This lint catches the whole class at CI, offline, instantly."""
import re
from pathlib import Path

_UW = Path(__file__).parent.parent / "server" / "uw.py"


def test_uw_api_paths_use_hyphens_not_underscores():
    src = _UW.read_text(encoding="utf-8")
    path_literals = re.findall(r"""["']/api/[^"']*["']""", src)
    assert path_literals, "expected /api/ path literals in server/uw.py"
    offenders = []
    for lit in path_literals:
        # strip the quotes and any {placeholder} interpolations, then check the path
        body = re.sub(r"\{[^}]*\}", "", lit).strip("\"'")
        if "_" in body:
            offenders.append(lit)
    assert not offenders, (
        "UW API paths must be hyphenated, not underscored (underscore → 404 → silent "
        f"degrade). Offending path literals in server/uw.py: {offenders}")
