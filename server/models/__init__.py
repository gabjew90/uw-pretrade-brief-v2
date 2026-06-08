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


# ── Signals (Derive stage output) — first-class entities, fields per instructions ──
class Signal(BaseModel):
    """Base for every derived signal: a value + how confident + where it came from.
    Subclasses add their own typed fields. NEVER add I/O — Derive is pure."""
    provenance: Provenance = Field(default_factory=Provenance)


class Flow(Signal):
    """Opening-flow direction/conviction. Fields TBD per instructions."""


class Positioning(Signal):
    """OI confirmation of the flow's bet. Fields TBD."""


class DealerGamma(Signal):
    """Dealer-gamma / GEX structural regime (flip, walls). Fields TBD."""


class Skew(Signal):
    """25Δ risk-reversal skew leg. Fields TBD."""


class Cost(Signal):
    """IV-rank / event / expected-move-vs-cost guard. Fields TBD."""


class Regime(Signal):
    """Market-wide regime read (NEVER a direction call). Fields TBD."""


# ── Verdict (Decide stage output) — built in exactly one place ───────────────
class Verdict(BaseModel):
    """The single decision. Consumes signals BY NAME; emits an action + named reasons,
    so a computed-but-unused signal is a visible gap, not a silent strand."""
    action: str = ""                       # e.g. "Stand down" / "Favorable: calls"
    reasons: list[str] = Field(default_factory=list)
    signals_used: list[str] = Field(default_factory=list)  # names consumed (audit)
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
    tone: Literal["up", "down", "neutral", "warn"] = "neutral"


class ViewModel(BaseModel):
    """Everything one ticker's deep-dive needs to render. Server-built, client-dumb."""
    ticker: str
    as_of: Optional[str] = None
    elements: list[Element] = Field(default_factory=list)
    verdict: Optional[Verdict] = None
