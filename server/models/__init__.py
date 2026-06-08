"""Domain model — first-class entities + the cross-stage typed contracts.

Two kinds of types live here:

1. **Provenance** — the universal quality/source/as_of stamp every value carries
   (Stage boundaries pass provenance, not bare values).
2. **Domain entities** — Flow, Positioning, DealerGamma, Skew, Cost, Regime, Verdict
   (signals) and the **ViewModel/Element** the frontend renders. Field bodies are
   intentionally minimal stubs here — they get filled per operator instructions — but
   the *shapes and boundaries* are fixed so stages can be wired now.

The frontend renders `ViewModel` and computes NOTHING.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Provenance — a type, not scattered flags ─────────────────────────────────
class Source(str, Enum):
    LIVE = "live"          # fetched from UW this request
    CACHE = "cache"        # RAM/duckdb cache within TTL
    ARCHIVE = "archive"    # older bronze partition (replay / fallback)
    DERIVED = "derived"    # computed from other provenanced values


class Quality(str, Enum):
    REAL = "real"              # trustworthy
    DEGRADED = "degraded"      # partial / stale / proxy — usable with caution
    UNAVAILABLE = "unavailable"  # could not be obtained; do NOT fabricate


class Provenance(BaseModel):
    source: Source = Source.DERIVED
    quality: Quality = Quality.REAL
    as_of: Optional[str] = None     # ISO-8601 of the OLDEST input (worst-case freshness)
    note: str = ""                  # short human reason when degraded/unavailable

    @staticmethod
    def worst(*provs: "Provenance") -> "Provenance":
        """Merge several provenances into the worst-case (for derived values)."""
        order_q = {Quality.REAL: 0, Quality.DEGRADED: 1, Quality.UNAVAILABLE: 2}
        order_s = {Source.LIVE: 0, Source.CACHE: 1, Source.ARCHIVE: 2, Source.DERIVED: 3}
        provs = [p for p in provs if p] or [Provenance()]
        q = max(provs, key=lambda p: order_q[p.quality]).quality
        s = max(provs, key=lambda p: order_s[p.source]).source
        as_ofs = sorted([p.as_of for p in provs if p.as_of])
        return Provenance(source=s, quality=q, as_of=as_ofs[0] if as_ofs else None)


# ── Canonical records (Normalize stage output) — validated, the ONLY thing Derive reads ──
class FlowAlert(BaseModel):
    """One option flow-alert, validated. Normalize maps a raw UW row → this; the types
    here are the contract Derive may rely on. Field-name resolution (type/option_type,
    total_premium-as-string) happens in normalize; this model enforces the final shape.

    Required fields are the ones v2 VERIFIED on real payloads (`e1d6c5e:server/uw.py`
    `flow_records`: `type`, `total_premium`, `created_at`). The rest are documented-but-
    unconfirmed UW fields kept optional until Phase-2 golden bronze tightens them — an
    absent optional is `None` (honest), never a fabricated default.
    """
    model_config = ConfigDict(extra="ignore")

    ticker: str
    type: Literal["call", "put"]            # normalized from UW `type`/`option_type`
    total_premium: float                    # UW sends this as a string; coerced here
    created_at: str                         # ISO-8601 timestamp of the alert
    # opening-flow signal (Phase 3 direction): volume/OI > 1 ⇒ opening trade
    volume_oi_ratio: Optional[float] = None
    strike: Optional[float] = None
    expiry: Optional[str] = None
    total_ask_side_prem: Optional[float] = None
    total_bid_side_prem: Optional[float] = None
    has_sweep: Optional[bool] = None
    has_singleleg: Optional[bool] = None
    has_multileg: Optional[bool] = None
    # set by normalize when the pull hit the 500 page-cap (session tail may be missing);
    # a per-pull property stamped on each row so Derive/Present can surface it honestly.
    truncated: bool = False
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("type", mode="before")
    @classmethod
    def _norm_side(cls, v: Any) -> Any:
        """Map UW side spellings (C/CALL/call, P/PUT/put) to the canonical literal.
        A value that isn't recognisably a side is left unchanged so validation FAILS
        loudly (a silent miscategorisation is the bug class this boundary exists to kill)."""
        if isinstance(v, str):
            s = v.strip().lower()
            if s.startswith("c"):
                return "call"
            if s.startswith("p"):
                return "put"
        return v


class GreekFlowPoint(BaseModel):
    """One per-MINUTE greek-flow row. `dir_delta_flow` is directional (single-leg) delta
    being traded that minute; `total_delta_flow` is all-flow incl. hedges. PER-MINUTE —
    the session figure is sum(...), the curve is cumsum(...) (never a last-tick scalar).
    Sign PINNED Phase 2 (golden-bronze finding (a)): positive dir_delta_flow = bullish/
    call, negative = bearish/put. Values arrive as decimal strings; coerced here."""
    model_config = ConfigDict(extra="ignore")

    timestamp: str
    dir_delta_flow: float
    total_delta_flow: Optional[float] = None
    provenance: Provenance = Field(default_factory=Provenance)


# ── Signals (Derive stage output) — first-class entities, fields per instructions ──
class Signal(BaseModel):
    """Base for every derived signal: a value + how confident + where it came from.
    Subclasses add their own typed fields. NEVER add I/O — Derive is pure."""
    provenance: Provenance = Field(default_factory=Provenance)


class Flow(Signal):
    """Opening-flow direction. `direction` is the call/put side the money is betting;
    `direction_basis` records HOW it was derived (opening_flow leads per Ge-Lin-Pearson;
    total_flow is the fallback; gamma_fallback arrives in Phase 4; unavailable = no flow,
    never a guessed side). call_prem/put_prem are the premium totals the side won by."""
    direction: Optional[Literal["calls", "puts"]] = None
    direction_basis: Literal["opening_flow", "total_flow", "gamma_fallback", "unavailable"] = "unavailable"
    call_prem: float = 0.0
    put_prem: float = 0.0
    truncated: bool = False   # the flow-alerts pull hit the page cap (window may be partial)


class IVTermPoint(BaseModel):
    """One point of the interpolated-IV term structure. `percentile` is the IV RANK
    (0–1; 0.50 = median vs the ticker's own history) — the cost gate's input.
    `implied_move_perc` is the expected move for that horizon. Strings coerced."""
    model_config = ConfigDict(extra="ignore")

    date: str
    days: int
    percentile: Optional[float] = None         # IV rank, 0..1
    volatility: Optional[float] = None
    implied_move_perc: Optional[float] = None
    provenance: Provenance = Field(default_factory=Provenance)


class SkewPoint(BaseModel):
    """One day's 25Δ risk-reversal (from historical-risk-reversal-skew). `risk_reversal`
    is the VENDOR convention = put_IV − call_IV (positive = put-skew). Derive sign-corrects
    to call−put. Values arrive as decimal strings; coerced here."""
    model_config = ConfigDict(extra="ignore")

    date: str
    delta: float = 25.0
    risk_reversal: float                       # vendor: put_IV - call_IV (positive = put-skew)
    provenance: Provenance = Field(default_factory=Provenance)


class OISnapshot(BaseModel):
    """One per-strike open-interest row (from oi-per-strike). `call_oi`/`put_oi` are the
    standing positions at that strike for a session. `provisional=True` = today's OI, not
    yet settled (settles ~9:15 ET next session — the clock gates this); settled rows are
    the only ones positioning trends. Strings coerced."""
    model_config = ConfigDict(extra="ignore")

    date: str
    strike: float
    call_oi: int = 0
    put_oi: int = 0
    provisional: bool = False
    provenance: Provenance = Field(default_factory=Provenance)


class GammaStrike(BaseModel):
    """One per-strike row of dealer gamma exposure (from spot-exposures/strike). UW
    PRE-SIGNS `put_gamma_oi` negative, so net dealer gamma at a strike is the signed SUM
    `call_gamma_oi + put_gamma_oi` (NOT a difference — subtracting double-negates puts and
    breaks the flip; the 2026-05-30 bug). `price` is the underlying spot, repeated per row."""
    model_config = ConfigDict(extra="ignore")

    strike: float
    call_gamma_oi: float = 0.0
    put_gamma_oi: float = 0.0
    price: Optional[float] = None
    provenance: Provenance = Field(default_factory=Provenance)


class Conviction(Signal):
    """Greek-flow directional conviction — SAME flow family as Flow (signal-honesty
    §Confluence), so in the funnel it only acts on DIVERGENCE vs flow, never as an
    agreement bonus. `direction` is the sign of the session dir_delta_flow net (pinned:
    positive=calls/bullish). `accumulation` reads the cumsum path shape."""
    direction: Optional[Literal["calls", "puts"]] = None
    dir_delta: float = 0.0          # session net of dir_delta_flow (directional bets)
    total_delta: float = 0.0        # session net of total_delta_flow (all-flow, secondary)
    accumulation: Literal["building", "fading", "choppy", "reversed", "flat"] = "flat"
    efficiency: float = 0.0


class Positioning(Signal):
    """OI confirmation of the flow's bet — does the flow-side near-dated OI cluster GROW
    (building) or shrink (unwinding) across settled sessions? NOT a direction (it confirms
    the EXISTING flow side). `unconfirmed` (missing history) NEVER blocks — archive-
    decoupled (tile2-confirmation-principle). decide collapses Flow + this via
    positioning_leg."""
    confirmation: Literal["building", "flat", "unwinding", "unconfirmed"] = "unconfirmed"
    oi_trend_pct: float = 0.0                    # cluster OI change, first→last settled session
    side: Literal["call", "put", ""] = ""        # the flow side this confirms
    cluster_strikes: list[float] = Field(default_factory=list)


class DealerGamma(Signal):
    """Dealer-gamma / GEX structural regime. A GUARD, not a direction (decide spec): POS
    gamma = dealers pin/mean-revert (caps a directional verdict); NEG = amplify/trend.
    `flip_pct` = distance of the gamma flip from spot (%); walls bound the expected range."""
    gex_sign: Literal["POS", "NEG"] = "POS"
    flip_pct: float = 0.0                       # (flip - spot) / spot * 100
    flip_status: Literal["ok", "no_flip", "unavailable"] = "unavailable"
    call_wall_pct: float = 0.0                  # call wall distance above spot (%)
    put_wall_pct: float = 0.0                   # put wall distance below spot (%)
    agg_b: float = 0.0                          # aggregate net gamma, $bn (signed)


class Skew(Signal):
    """25Δ risk-reversal skew — the ORTHOGONAL directional leg (vol surface, a different
    mechanism than flow). `rr25` is SIGN-CORRECTED to call−put convention: >0 = call-skew
    (bullish lean), <0 = put-skew (defensive). `lean` is the raw read; whether it AGREES
    or OPPOSES is computed in decide vs the flow direction (asymmetric oppose-veto)."""
    rr25: Optional[float] = None
    lean: Literal["call_skew", "put_skew", "neutral", "unavailable"] = "unavailable"


class Cost(Signal):
    """Non-directional GUARD (decide spec): is it expensive/risky to buy premium now?
    `guard=block` (rich IV, or earnings/macro event in the hold window) → Stand down;
    `caution` → contributes to Mixed; `ok` → no objection. Never a direction."""
    guard: Literal["ok", "caution", "block"] = "ok"
    ivr: Optional[float] = None                 # IV rank 0–100
    days_to_earnings: Optional[int] = None
    event_within_hold: bool = False             # macro (FOMC/CPI/jobs) inside the hold window
    reason: str = ""


class Regime(Signal):
    """Market-wide regime read — trend vs chop, calm vs fearful, event vs clear. A POSTURE
    (Favorable/Mixed/Stand down), mirroring the per-ticker verdict at the market level, and
    NEVER a direction call (asserted in tests). `event_within_hold` also caps the per-ticker
    Cost gate. Ported from `e1d6c5e:server/market_regime.py`."""
    posture: Literal["Favorable", "Mixed", "Stand down"] = "Mixed"
    headline: str = ""
    vol_line: str = ""
    event_line: Optional[str] = None
    event_severity: Optional[Literal["veto", "warn"]] = None
    event_within_hold: bool = False
    tide_badge: str = ""
    opex: bool = False


# ── Verdict (Decide stage output) — built in exactly one place ───────────────
class Verdict(BaseModel):
    """The single decision. Consumes signals BY NAME; emits an action + named reasons,
    so a computed-but-unused signal is a visible gap, not a silent strand."""
    action: str = ""                       # e.g. "Stand down" / "Favorable: calls"
    overall: Literal["Favorable", "Mixed", "Stand down"] = "Mixed"
    direction: Optional[Literal["calls", "puts"]] = None   # the side, owned by positioning/flow
    reasons: list[str] = Field(default_factory=list)
    signals_used: list[str] = Field(default_factory=list)  # names consumed (audit)
    signal_conflict: bool = False
    conflict_legs: list[str] = Field(default_factory=list)  # which legs disagree (tone cue)
    provenance: Provenance = Field(default_factory=Provenance)


# ── Present stage output — the view model the frontend renders ────────────────
class Element(BaseModel):
    """One renderable unit. The frontend shows `surface` + `label`, reveals `detail`
    on tap, and tints by `provenance.quality`. It computes NOTHING."""
    key: str
    label: str = ""                         # plain-English, novice-readable
    surface: Any = None                     # the glanceable value (string/number/struct)
    detail: Any = None                      # tap payload (secondary numbers, graphs)
    provenance: Provenance = Field(default_factory=Provenance)
    # the ONLY sentiment cue the frontend acts on; it never reads signal values to render
    tone: Literal["positive", "cautionary", "negative", "neutral", "unavailable"] = "neutral"


class ViewModel(BaseModel):
    """Everything one ticker's deep-dive needs to render. Server-built, client-dumb."""
    ticker: str
    as_of: Optional[str] = None
    elements: list[Element] = Field(default_factory=list)
    verdict: Optional[Verdict] = None
