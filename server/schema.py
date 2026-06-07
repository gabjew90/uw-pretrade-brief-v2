"""Pydantic models for the snapshot payload.

Matches the JSON shape the prototype JS consumes (no client-side normalization needed).

Note on _failures / _timestamp: Pydantic 2 rejects leading-underscore field names as
formal fields (raises NameError). Both are declared as extra fields instead — Row uses
ConfigDict(extra="allow"), so kwargs like `_failures=[...]` land in __pydantic_extra__
and remain accessible as `row._failures`. They serialize correctly via model_dump().
"""
from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Gates(BaseModel):
    flow: Literal["green", "yellow", "red"]
    oi: Literal["green", "yellow", "red"]
    structural: Literal["green", "yellow", "red"]
    cost: Literal["green", "yellow", "red"]


class GateMethod(BaseModel):
    flow: Literal["cross_sectional", "absolute", "percentile"]
    oi: Literal["cross_sectional", "absolute", "percentile"]
    structural: Literal["cross_sectional", "absolute", "percentile"]
    cost: Literal["cross_sectional", "absolute", "percentile"]


class Verdict(BaseModel):
    """Deep-dive 3-leg verdict (Plan 3). None on light/grid rows. See server/verdict.py."""
    positioning: Literal["green", "yellow", "red"]
    structural: Literal["green", "yellow", "red"]
    skew: Literal["agree", "oppose", "neutral", "unavailable"]
    cost_guard: Literal["ok", "caution", "block"]
    signal_conflict: bool = False
    conflict_legs: list[str] = Field(default_factory=list)
    overall: Literal["Favorable", "Mixed", "Stand down"]
    action: str
    rr25: float | None = None


class Flow(BaseModel):
    alerts: int = 0
    premium_usd: float = 0.0
    rank_cross: int = 100


class OIStrike(BaseModel):
    strike: float
    prev: int
    today: int
    pct: float


class OI(BaseModel):
    strikes: list[OIStrike] = Field(default_factory=list)


class OISessionBar(BaseModel):
    """One day's OI for a strike — a single bar in Tile 2's grouped chart."""
    date: str
    oi: int
    provisional: bool = False   # True for today (OI not settled until ~9am next session)


class StrikeOIHistory(BaseModel):
    """5-session OI progression for one (strike, side) + its positioning reads.
    Tile 2 shows ONE of these at a time — the (strike, side) with the most flow
    premium for the toggled side. `expiry` is the top expiry by $ at this strike+side
    (the OI bars themselves are that strike's call/put OI pooled across expiries —
    UW's oi-per-strike carries no expiry)."""
    strike: float
    side: Literal["call", "put", ""] = ""       # which side's OI these bars are
    expiry: str = ""                            # top expiry by $ for this strike+side (label)
    sessions: list[OISessionBar] = Field(default_factory=list)  # oldest→newest (SETTLED days only)
    delta_oi: int = 0           # newest-settled vs prior settled session OI change
    premium_usd: float = 0.0    # flow premium at this (strike, side) — the "money" ranking
    trend: Literal["building", "flat", "unwinding"] = "flat"
    today_vol_oi: float = 0.0   # today's volume/OI ratio at this strike — live, "confirms ~9am"


class ExpirySegment(BaseModel):
    """One slice of Tile 2's horizontal expiry-distribution bar."""
    expiry: str
    premium_usd: float
    pct: float                  # share of total flow premium (0-100)


class Tile2(BaseModel):
    """Positioning Reality Check — is the flow real, building, where, still held?"""
    # The OBSERVED dominant side this tile confirmed (flow-derived at build time).
    # Anchors every reading below — it is NOT the operator's direction toggle. The
    # toggle is your *choice*; flow_side is what the market is actually doing, so
    # the disagreement between them is the signal (never toggle it away). "" = no
    # flow observed (gamma-only direction).
    flow_side: Literal["call", "put", ""] = ""
    near_dated_pct: float = 0.0         # % of flow $ in the hold window (≤45 DTE) — relevance check
    oi_trend_5d_pct: float = 0.0        # flow-side cluster OI change (first→last settled session)
    confirmation: Literal["building", "flat", "unwinding", "unconfirmed"] = "unconfirmed"
    # BOTH clusters always — the call-vs-put comparison IS the signal, shown
    # regardless of the operator's side choice. Each is the 5-session OI trend
    # aggregated over that side's flow-hit strike cluster (near-dated).
    call_confirmation: Literal["building", "flat", "unwinding", "unconfirmed"] = "unconfirmed"
    put_confirmation: Literal["building", "flat", "unwinding", "unconfirmed"] = "unconfirmed"
    call_oi_trend_pct: float = 0.0
    put_oi_trend_pct: float = 0.0
    # Aggregate OI per settled session, summed across each side's flow-hit cluster
    # (the primary "did it build" visual — the verdict above is derived from this).
    call_sessions: list[OISessionBar] = Field(default_factory=list)
    put_sessions: list[OISessionBar] = Field(default_factory=list)
    # Settlement state (drives Tile 2's FORMING/SETTLED badge). Keyed to the TRADING
    # calendar, NOT calendar "today": a weekend/holiday is never a forming session.
    # "forming" = the flow's trading session whose OI hasn't published yet (it
    # confirms ~9:15am ET the NEXT trading day = settles_on); "settled" = it landed
    # as a bar and confirms (or kills) that session's flow.
    settlement_mode: Literal["forming", "settled"] = "settled"
    settled_through: str = ""            # newest settled bar date (ISO)
    forming_date: str = ""              # the unsettled trading-session date, "" when none
    settles_on: str = ""               # next trading day its OI publishes (ISO), "" when settled
    sessions_available: int = 0         # settled days available (target 5)
    strikes: list[StrikeOIHistory] = Field(default_factory=list)
    expiry_distribution: list[ExpirySegment] = Field(default_factory=list)
    low_conviction: bool = False
    low_conviction_msg: str = ""


class Tile3Strike(BaseModel):
    """One rung of Tile 3's gamma ladder: net dealer gamma at a strike
    (call_gamma_oi - put_gamma_oi). Positive = dealers long γ there."""
    strike: float
    net_gamma: float


class Tile3(BaseModel):
    """Structural Setup ladder — real per-strike net dealer gamma near spot.
    Empty when the greek-exposure fetch failed/warming; the renderer then falls
    back to its synthetic bars."""
    strikes: list[Tile3Strike] = Field(default_factory=list)


class DarkPool(BaseModel):
    net_premium_usd: float = 0.0
    pct_of_volume: int = 0
    trend: Literal["buying", "selling", "neutral"] = "neutral"


class NewsItem(BaseModel):
    time: str
    source: str
    headline: str


class Insights(BaseModel):
    structural: str | None = None
    curve: str | None = None
    flow: str | None = None         # Tile 1 narration of the OBSERVED flow tape (not the toggle)
    positioning: str | None = None  # Tile 2 narration of the OI-confirmation reality


class FlowAlert(BaseModel):
    """Per-alert detail powering Tile 1's scatter and Tile 2's positioning reads.
    The flow-alerts fetch is shared between both tiles — the volume / OI fields
    aren't drawn in Tile 1 (it's at its 6-channel ceiling) but Tile 2 reads
    them, so we capture them on the same call.

    All numeric fields are floats so JS doesn't have to parse the UW string-
    typed payload (`total_premium` etc. arrive as strings)."""
    created_at: str           # ISO-8601 UTC; JS converts to ET for display
    strike: float
    type: Literal["call", "put"]
    total_premium: float
    total_ask_side_prem: float
    total_bid_side_prem: float
    has_sweep: bool = False
    has_singleleg: bool = True
    has_multileg: bool = False
    underlying_price: float = 0.0
    option_chain: str = ""
    expiry: str = ""
    total_size: int = 0
    # Tile 2 inputs (captured on this fetch; not drawn in Tile 1 except for
    # the opening tag in the per-bubble hover).
    all_opening_trades: bool = False
    volume_oi_ratio: float = 0.0
    volume: int = 0
    open_interest: int = 0


class OHLCBar(BaseModel):
    """One OHLC candle — feeds Tile 1's gray price line. `t` is epoch-seconds
    (UTC) so JS can render a time-vs-price line without parsing strings."""
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


class GreekFlow(BaseModel):
    """Tile 1 net-delta composite (display-only, deep-dive only). Per-minute greek
    FLOW aggregated over the session. LEAD = dir_delta (directional single-leg bets,
    the strategy-relevant read); net_delta (total_delta_flow, all flow incl. hedges)
    is the secondary cross-read. The directional sign is NOT independently validated,
    so the render leads cautiously and stands down on divergence rather than
    asserting a confident call. See spec 2026-06-06-tile1-greek-flow-delta."""
    available: bool = False
    dir_delta: float = 0.0      # Σ dir_delta_flow — directional bets (headline/LED). +=bullish
    net_delta: float = 0.0      # Σ total_delta_flow — all-flow incl. hedges (secondary cross-read)
    accumulation: Literal["building", "fading", "reversed", "choppy", "flat"] = "flat"
    efficiency: float = 0.0     # |final| / max|cumsum| — path cleanliness (0-1)
    cumsum: list[float] = Field(default_factory=list)  # downsampled DIRECTIONAL curve (sparkline)
    session_date: str = ""      # ET date of the data
    provisional: bool = False   # True when live & early in the session (read not yet trustworthy)


class Row(BaseModel):
    # extra="allow" lets _failures and _timestamp pass through as extra fields.
    # Pydantic 2 forbids leading-underscore formal field names, so this is the
    # correct approach: `Row(..., _failures=["darkpool"])` stores them in
    # __pydantic_extra__ and exposes them as row._failures.
    model_config = ConfigDict(extra="allow")

    ticker: str
    spot: float
    direction: Literal["calls", "puts"]
    direction_basis: Literal["opening_flow", "total_flow", "gamma_fallback"] = "gamma_fallback"
    is_synthetic: bool = False
    is_light: bool = False   # True = flow-only grid row (no per-ticker gamma/OI/cost);
                             # full gates + tiles fill in on click. See basic-platform spec.
    verdict: Verdict | None = None   # deep-dive 3-leg verdict; None on light/grid rows
    gates: Gates
    gate_method: GateMethod
    flow: Flow = Field(default_factory=Flow)
    oi: OI = Field(default_factory=OI)
    flip_dist_pct: float = 0.0
    wall_up_dist_pct: float = 0.0
    wall_dn_dist_pct: float = 0.0
    agg_gamma_b: float = 0.0
    gex_sign: Literal["POS", "NEG"] = "POS"
    # Structural-read availability: "ok" = real γ flip in range; "no_flip" = γ
    # doesn't cross zero within the window (don't draw a flip level); "unavailable"
    # = no greek-exposure data for this ticker. Drives gate neutralization + the
    # Tile 3 "limited" state so we never present a fabricated structural read.
    gex_status: Literal["ok", "no_flip", "unavailable"] = "ok"
    ivr: int = 50
    days_to_earnings: int | None = None
    iv_term_curve: list[float] = Field(default_factory=list)
    sector: str = ""
    sector_tide_value: float = 0.0
    dark_pool: DarkPool = Field(default_factory=DarkPool)
    news_items: list[NewsItem] = Field(default_factory=list)
    insights: Insights = Field(default_factory=Insights)
    # Per-alert detail + OHLC for Tile 1's scatter-over-line chart. Empty when
    # the upstream fetches haven't run / failed.
    flow_alerts_detail: list[FlowAlert] = Field(default_factory=list)
    ohlc: list[OHLCBar] = Field(default_factory=list)
    # Aggregate of total_ask_side_prem / (ask + bid) across alerts. 0.0 when
    # the ask/bid premium fields are empty on this tier.
    ask_side_pct: float = 0.0
    tile2: Tile2 = Field(default_factory=Tile2)
    tile3: Tile3 = Field(default_factory=Tile3)
    greek_flow: GreekFlow = Field(default_factory=GreekFlow)   # Tile 1 net-delta composite (deep-dive)


class Regime(BaseModel):
    # NOTE: the pre-data-driven stubs (label/detail/vix) were removed — posture +
    # headline + vol_line/event_line/tide_badge are the live read, and the banner
    # color is driven by `posture`, not an env-var `label`.
    headline: str = ""
    posture: Literal["Favorable", "Mixed", "Stand down", ""] = ""
    event_line: str | None = None
    vol_line: str = ""
    tide_badge: str = ""
    opex: bool = False
    as_of: str | None = None   # ISO time the regime was computed; lets the front
                               # door age it independently of the flow grid.
    # Raw evidence behind the posture (so the verdict is sanity-checkable, not just
    # asserted). All honest-None when the underlying input was unavailable.
    spy_spot: float | None = None
    flip_pct: float | None = None              # γ-flip distance from spot, %; strike = spot*(1+flip_pct/100)
    gex_sign: Literal["POS", "NEG", ""] = ""   # dealer-gamma sign (only when the read is trustworthy)
    iv: float | None = None                    # SPY implied vol, decimal annualized
    rv: float | None = None                    # SPY realized vol, decimal annualized
    tide_value: float | None = None            # market-tide net premium, $


class Snapshot(BaseModel):
    fetched_at: datetime
    regime: Regime
    rows: list[Row]
    stale_since: datetime | None = None
