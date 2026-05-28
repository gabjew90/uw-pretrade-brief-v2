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
    ivr: int = 50
    days_to_earnings: int | None = None
    iv_term_curve: list[float] = Field(default_factory=list)
    sector: str = ""
    sector_tide_value: float = 0.0
    dark_pool: DarkPool = Field(default_factory=DarkPool)
    news_items: list[NewsItem] = Field(default_factory=list)
    insights: Insights = Field(default_factory=Insights)


class Regime(BaseModel):
    label: Literal["normal", "risk-off"] = "normal"
    detail: str = ""
    vix: float = 0.0


class Snapshot(BaseModel):
    fetched_at: datetime
    regime: Regime
    rows: list[Row]
    stale_since: datetime | None = None
