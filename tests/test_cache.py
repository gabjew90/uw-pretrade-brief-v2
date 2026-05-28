"""Tests for in-memory cache primitives."""
from __future__ import annotations
import time
import pytest
from server.cache import TTLCache


def test_set_and_get_returns_value():
    c = TTLCache()
    c.set("k", "v", ttl_seconds=10)
    assert c.get("k") == "v"


def test_get_after_expiry_returns_none():
    c = TTLCache()
    c.set("k", "v", ttl_seconds=0.05)
    time.sleep(0.10)
    assert c.get("k") is None


def test_get_missing_key_returns_none():
    c = TTLCache()
    assert c.get("nope") is None


def test_set_overwrites_previous_value():
    c = TTLCache()
    c.set("k", "v1", ttl_seconds=10)
    c.set("k", "v2", ttl_seconds=10)
    assert c.get("k") == "v2"


def test_ttl_is_per_key():
    c = TTLCache()
    c.set("short", "s", ttl_seconds=0.05)
    c.set("long", "l", ttl_seconds=10)
    time.sleep(0.10)
    assert c.get("short") is None
    assert c.get("long") == "l"
