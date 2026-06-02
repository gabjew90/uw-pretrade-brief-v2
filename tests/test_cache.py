"""Tests for in-memory cache primitives."""
from __future__ import annotations
import time
from datetime import datetime, timezone

from server.cache import TTLCache


def test_set_and_get_returns_value():
    c = TTLCache()
    c.set("k", "v", ttl_seconds=10)
    value, _ = c.get("k")
    assert value == "v"


def test_get_after_expiry_returns_none_pair():
    c = TTLCache()
    c.set("k", "v", ttl_seconds=0.05)
    time.sleep(0.10)
    assert c.get("k") == (None, None)


def test_get_missing_key_returns_none_pair():
    c = TTLCache()
    assert c.get("nope") == (None, None)


def test_set_overwrites_previous_value():
    c = TTLCache()
    c.set("k", "v1", ttl_seconds=10)
    c.set("k", "v2", ttl_seconds=10)
    value, _ = c.get("k")
    assert value == "v2"


def test_ttl_is_per_key():
    c = TTLCache()
    c.set("short", "s", ttl_seconds=0.05)
    c.set("long", "l", ttl_seconds=10)
    time.sleep(0.10)
    assert c.get("short") == (None, None)
    value, _ = c.get("long")
    assert value == "l"


def test_get_returns_value_and_observed_at():
    c = TTLCache()
    obs = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    c.set("k", {"v": 1}, ttl_seconds=60, observed_at=obs)
    value, observed_at = c.get("k")
    assert value == {"v": 1}
    assert observed_at == obs


def test_get_miss_returns_none_pair():
    c = TTLCache()
    assert c.get("absent") == (None, None)


def test_observed_at_defaults_to_none_when_unset():
    c = TTLCache()
    c.set("k", 5, ttl_seconds=60)            # no observed_at
    value, observed_at = c.get("k")
    assert value == 5 and observed_at is None
