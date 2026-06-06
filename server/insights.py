"""Gemini wrapper for chart insights with 5-min cache + deterministic fallback.

Two prompts:
  - structural (3 sentences, Tile 3)
  - curve (1 sentence, Tile 4)

Cache key is lossy on purpose: insights only re-fire when the underlying
state moves enough to matter (gate flip, flip-distance ±0.5%, walls ±1).
"""
from __future__ import annotations
import logging
import os
from pathlib import Path

from server.cache import TTLCache

log = logging.getLogger(__name__)

_INSIGHT_TTL_SECONDS = 300  # 5 min
_insight_cache = TTLCache()


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_STRUCTURAL_TEMPLATE = (_PROMPTS_DIR / "structural.txt").read_text(encoding="utf-8")
_CURVE_TEMPLATE = (_PROMPTS_DIR / "curve.txt").read_text(encoding="utf-8")
_FLOW_TEMPLATE = (_PROMPTS_DIR / "flow.txt").read_text(encoding="utf-8")


def generate_insights(row: dict) -> dict[str, str | None]:
    return {
        "structural": _generate_or_cache(row, "structural"),
        "curve": _generate_or_cache(row, "curve"),
        "flow": _generate_or_cache(row, "flow"),
    }


def _flow_facts(row: dict) -> dict:
    """Pull the OBSERVED flow-tape story off the row for Tile 1 narration —
    anchored to the flow-derived side (tile2.flow_side), never an operator toggle.
    Tile 1 scope ONLY: side, concentration, premium, sweeps, ask/bid aggression.
    Open interest / positioning confirmation is Tile 2's job — not narrated here."""
    t2 = row.get("tile2") or {}
    side = t2.get("flow_side") or ("put" if row.get("direction") == "puts" else "call")
    dominant = "puts" if side == "put" else "calls"
    strikes = [s for s in (t2.get("strikes") or []) if s.get("side") == side]
    top = strikes[0] if strikes else {}
    prem = float((row.get("flow") or {}).get("premium_usd", 0.0) or 0.0)
    prem_str = f"${prem / 1e6:.1f}M" if prem >= 1e6 else f"${round(prem / 1e3)}k"
    ask_pct = round(float(row.get("ask_side_pct", 0.0) or 0.0) * 100)
    sweeps = sum(1 for a in (row.get("flow_alerts_detail") or [])
                 if a.get("type") == side and a.get("has_sweep"))
    ts = top.get("strike")
    return {
        "ticker": row.get("ticker", "TICKER"),
        "dominant": dominant,
        "top_strike": f"${ts:g}" if ts else "the focus",
        "top_expiry": top.get("expiry") or "near-dated",
        "premium": prem_str,
        "ask_pct": f"{ask_pct}",
        "ask_side": "mostly at the ask (lifting offers)" if ask_pct >= 55
                    else "mostly at the bid (hitting bids)" if ask_pct and ask_pct <= 45
                    else "mixed ask/bid",
        "sweeps": sweeps,
        "sweep_clause": f", incl. {sweeps} swept" if sweeps else "",
    }


def _generate_or_cache(row: dict, kind: str) -> str:
    key = _cache_key(row, kind)
    cached, _ = _insight_cache.get(key)
    if cached is not None:
        return cached
    text = _generate(row, kind)
    _insight_cache.set(key, text, ttl_seconds=_INSIGHT_TTL_SECONDS)
    return text


def _generate(row: dict, kind: str) -> str:
    # Honest structural states bypass Gemini entirely — there's no real flip to
    # narrate, so don't let the model invent one.
    if kind == "structural" and row.get("gex_status", "ok") != "ok":
        return _structural_status_msg(row)
    if not os.environ.get("GEMINI_API_KEY"):
        return _fallback(row, kind)
    try:
        client = _get_client()
        prompt = _render_prompt(row, kind)
        return client.generate(prompt)
    except Exception as e:
        log.warning("Gemini call failed for %s/%s: %s — using fallback",
                    row.get("ticker"), kind, e)
        return _fallback(row, kind)


def _render_prompt(row: dict, kind: str) -> str:
    if kind == "flow":
        return _FLOW_TEMPLATE.format(**_flow_facts(row))
    if kind == "structural":
        flip = row.get("flip_dist_pct", 0.0)
        return _STRUCTURAL_TEMPLATE.format(
            ticker=row.get("ticker", "TICKER"),
            direction=row.get("direction", "calls"),
            flip_pct=f"{abs(flip):.1f}",
            flip_side="above" if flip >= 0 else "below",
            wall_up_pct=f"{row.get('wall_up_dist_pct', 0):.1f}",
            wall_dn_pct=f"{row.get('wall_dn_dist_pct', 0):.1f}",
        )
    # curve
    curve = row.get("iv_term_curve") or [0, 0]
    front_iv = curve[0] * 100 if curve else 0
    back_iv = curve[-1] * 100 if curve else 0
    spread_pts = front_iv - back_iv
    shape = "inverted" if spread_pts > 3 else "normal"
    return _CURVE_TEMPLATE.format(
        ticker=row.get("ticker", "TICKER"),
        front_iv=f"{front_iv:.0f}",
        back_iv=f"{back_iv:.0f}",
        curve_shape=shape,
        spread_pts=f"{abs(spread_pts):.1f}",
    )


def _cache_key(row: dict, kind: str) -> tuple:
    """Lossy: same key shared across small numeric jitter."""
    if kind == "flow":
        f = _flow_facts(row)
        return ("flow", f["ticker"], f["dominant"], f["top_strike"],
                round(float(row.get("ask_side_pct", 0.0) or 0.0) * 10),
                f["sweeps"])
    if kind == "structural":
        return (
            "structural",
            row.get("ticker"),
            row.get("gates", {}).get("structural", "?"),
            round(row.get("flip_dist_pct", 0) * 2) / 2,
            round(row.get("wall_up_dist_pct", 0)),
            round(row.get("wall_dn_dist_pct", 0)),
        )
    curve = row.get("iv_term_curve") or [0, 0]
    front_iv = curve[0] if curve else 0
    back_iv = curve[-1] if curve else 0
    return (
        "curve",
        row.get("ticker"),
        round(front_iv * 100),
        round(back_iv * 100),
    )


def _structural_status_msg(row: dict) -> str:
    """Deterministic, honest copy for the non-ok structural states (no fabricated
    flip). 'unavailable' = no greek-exposure data; 'no_flip' = γ doesn't cross
    zero within the window (walls may still be real)."""
    t = row.get("ticker", "this ticker")
    if row.get("gex_status") == "unavailable":
        return (f"Structural γ data is limited for {t} right now — no reliable "
                f"dealer-positioning read available.")
    regime = ("long γ (dealers dampen moves — pin)" if row.get("gex_sign") == "POS"
              else "short γ (dealers amplify moves — trend)")
    wu = row.get("wall_up_dist_pct", 0.0)
    wd = row.get("wall_dn_dist_pct", 0.0)
    return (f"No γ flip within ±20% of spot — dealers sit in <strong>{regime}</strong> "
            f"across the visible range. Walls bracket it at +{wu:.1f}% / −{wd:.1f}%.")


def _fallback(row: dict, kind: str) -> str:
    """Deterministic rules-based insight (same logic the v1 prototype used)."""
    if kind == "flow":
        f = _flow_facts(row)
        if f["premium"] in ("$0k", "$0M") and f["top_strike"] == "the focus":
            return "No material single-leg flow on either side yet — nothing to narrate."
        return (f"<strong>{f['dominant'].capitalize()}</strong> led, heaviest at "
                f"<strong>{f['top_strike']}</strong> ({f['top_expiry']}), "
                f"<strong>{f['premium']}</strong> premium{f['sweep_clause']}. "
                f"The tape printed {f['ask_side']} (<strong>{f['ask_pct']}%</strong> at ask) "
                f"— <em>{'initiated buying' if int(f['ask_pct'] or 0) >= 55 else 'sold into' if int(f['ask_pct'] or 0) and int(f['ask_pct']) <= 45 else 'two-way'}</em>.")
    if kind == "structural":
        flip = row.get("flip_dist_pct", 0.0)
        gate = row.get("gates", {}).get("structural", "yellow")
        verdict = ("favors" if gate == "green" else
                   "fights" if gate == "red" else "is neutral on")
        side = "above" if flip >= 0 else "below"
        return (f"Spot sits <strong>{abs(flip):.1f}% {side}</strong> the γ flip; "
                f"walls bracket the range at +{row.get('wall_up_dist_pct',0):.1f}% / "
                f"−{row.get('wall_dn_dist_pct',0):.1f}%. "
                f"Net: structure <em>{verdict}</em> the {row.get('direction')} trade.")
    curve = row.get("iv_term_curve") or [0, 0]
    front = curve[0] * 100 if curve else 0
    back = curve[-1] * 100 if curve else 0
    spread = front - back
    shape = ("INVERTED — front-month vol elevated" if spread > 3
             else "normal upward — no near-term vol premium")
    return f"Term structure {shape} (front <strong>{front:.0f}</strong> → back <strong>{back:.0f}</strong>)."


# ── Gemini client wrapper ─────────────────────────────────────────────────────

class _GeminiClient:
    def __init__(self, api_key: str):
        from google import genai
        self._client = genai.Client(api_key=api_key)

    def generate(self, prompt: str) -> str:
        response = self._client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt,
        )
        return (response.text or "").strip()


def _get_client() -> _GeminiClient:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    return _GeminiClient(key)
