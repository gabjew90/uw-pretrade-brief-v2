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
    """5-session OI progression for one strike + its positioning reads."""
    strike: float
    sessions: list[OISessionBar] = Field(default_factory=list)  # oldest→newest (SETTLED days only)
    delta_oi: int = 0           # newest-settled vs prior settled session OI change
    net_delta: float = 0.0      # per-strike net delta from greek-exposure (call+put delta_oi)
    premium_usd: float = 0.0    # flow premium concentrated at this strike (preferred $ label)
    trend: Literal["building", "flat", "unwinding"] = "flat"
    today_vol_oi: float = 0.0   # today's volume/OI ratio at this strike — live, "confirms ~9am"


class ExpirySegment(BaseModel):
    """One slice of Tile 2's horizontal expiry-distribution bar."""
    expiry: str
    premium_usd: float
    pct: float                  # share of total flow premium (0-100)


class Tile2(BaseModel):
    """Positioning Reality Check — is the flow real, building, where, still held?"""
    opening_pct: float = 0.0            # % of single-leg alerts flagged all_opening_trades
    avg_volume_oi_ratio: float = 0.0    # >1 = today's volume exceeds existing OI (opening intensity)
    oi_trend_5d_pct: float = 0.0        # aggregate OI change across available sessions
    confirmation: Literal["building", "flat", "unwinding", "unconfirmed"] = "unconfirmed"
    sessions_available: int = 0         # 1-5; <5 means archive still filling in
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


class Row(BaseModel):
    # extra="allow" lets _failures and _timestamp pass through as extra fields.
    # Pydantic 2 forbids leading-underscore formal field names, so this is the
    # correct approach: `Row(..., _failures=["darkpool"])` stores them in
    # __pydantic_extra__ and exposes them as row._failures.
    model_config = ConfigDict(extra="allow")

    ticker: str
    spot: float
    direction: Literal["calls", "puts"]
    is_synthetic: bool = False
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


class Regime(BaseModel):
    label: Literal["normal", "risk-off"] = "normal"
    detail: str = ""
    vix: float = 0.0


class Snapshot(BaseModel):
    fetched_at: datetime
    regime: Regime
    rows: list[Row]
    stale_since: datetime | None = None
