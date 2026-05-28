"""Tests for tracked-universe composition + sticky-set lifecycle."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from server import universe


def test_indices_constant_includes_majors():
    assert "SPX" in universe.INDICES
    assert "QQQ" in universe.INDICES
    assert "VIX" in universe.INDICES


def test_parse_pinned_handles_csv_env_var(monkeypatch):
    monkeypatch.setenv("TICKER_PIN_LIST", "AAPL, NVDA ,tsla,MSFT")
    pinned = universe.parse_pinned()
    assert pinned == {"AAPL", "NVDA", "TSLA", "MSFT"}, "uppercased + whitespace-stripped"


def test_parse_pinned_empty_when_env_unset(monkeypatch):
    monkeypatch.delenv("TICKER_PIN_LIST", raising=False)
    assert universe.parse_pinned() == set()


def test_sticky_state_touch_adds_new_tickers(tmp_data_dir):
    s = universe.StickyState({})
    now = datetime(2026, 5, 27, 14, 0, tzinfo=timezone.utc)
    s.touch(["NVDA", "AMD"], now=now)
    assert s.active(now=now) == {"NVDA", "AMD"}


def test_sticky_state_touch_refreshes_existing_timestamp(tmp_data_dir):
    old = datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc).isoformat()
    s = universe.StickyState({"NVDA": old})
    now = datetime(2026, 5, 27, 14, 0, tzinfo=timezone.utc)
    s.touch(["NVDA"], now=now)
    assert s._state["NVDA"] == now.isoformat()


def test_sticky_state_decay_drops_tickers_older_than_30_trading_days(tmp_data_dir):
    old = (datetime(2026, 5, 27, tzinfo=timezone.utc) - timedelta(days=50)).isoformat()
    recent = (datetime(2026, 5, 27, tzinfo=timezone.utc) - timedelta(days=10)).isoformat()
    s = universe.StickyState({"OLD": old, "RECENT": recent})
    now = datetime(2026, 5, 27, tzinfo=timezone.utc)
    s.decay(now=now)
    assert "RECENT" in s._state
    assert "OLD" not in s._state


def test_compose_universe_unions_all_sources(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TICKER_PIN_LIST", "AAPL,GOOGL")
    sticky = universe.StickyState({
        "NVDA": datetime(2026, 5, 27, tzinfo=timezone.utc).isoformat(),
    })
    hot_15 = ["TSLA", "AMD", "NVDA"]

    tracked = universe.compose_universe(hot_15=hot_15, sticky=sticky,
                                        now=datetime(2026, 5, 27, tzinfo=timezone.utc))
    assert "AAPL" in tracked and "GOOGL" in tracked
    assert "SPX" in tracked and "QQQ" in tracked
    assert "NVDA" in tracked
    assert "TSLA" in tracked and "AMD" in tracked


def test_top_15_unique_tickers_dedupes_and_caps():
    flow_alerts = {"data": [
        {"ticker": "NVDA"}, {"ticker": "NVDA"}, {"ticker": "AMD"},
        {"ticker": "TSLA"}, {"ticker": "PLTR"}, {"ticker": "GME"},
        {"ticker": "F"}, {"ticker": "BABA"}, {"ticker": "META"},
        {"ticker": "AAPL"}, {"ticker": "MSFT"}, {"ticker": "GOOGL"},
        {"ticker": "NFLX"}, {"ticker": "AMZN"}, {"ticker": "JPM"},
        {"ticker": "BAC"}, {"ticker": "WMT"},
    ]}
    result = universe.top_15_unique_tickers(flow_alerts)
    assert len(result) == 15
    assert result[0] == "NVDA"
    assert len(set(result)) == 15
