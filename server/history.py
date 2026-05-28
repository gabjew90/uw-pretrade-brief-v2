"""TickerHistory — v0.2 percentile-based gate evolution path (spec §8b).

Stubbed in v2: the class exists so callsites can pass `history=None` without
import errors. Real implementation will read from /data/raw/*.parquet via
duckdb and return percentile rankings against the ticker's own history.
"""
from __future__ import annotations


class TickerHistory:
    """v2 stub. Calls raise NotImplementedError; gates.py defends with
    `if history is None or history.days_available(...) < 30` → fallback."""

    def __init__(self, ticker: str):
        self.ticker = ticker

    def days_available(self, gate: str) -> int:
        raise NotImplementedError("TickerHistory not wired in v2 — see spec §8b")

    def percentile(self, value: float, window_days: int) -> float:
        raise NotImplementedError("TickerHistory not wired in v2 — see spec §8b")

    def oi_change_percentile(self, value: float, window_days: int) -> float:
        raise NotImplementedError("TickerHistory not wired in v2 — see spec §8b")
