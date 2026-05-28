"""In-memory TTL cache primitive. Used by storage layer (per-ticker TTL on UW
responses) and insights layer (5-min TTL on Gemini outputs).

Process-local only — no Redis. Cold restart clears all entries; that's
acceptable for this single-operator stack."""
from __future__ import annotations
import time
from typing import Any


class TTLCache:
    """Dict with per-entry TTL. Reads after expiry return None as a cache miss."""

    def __init__(self) -> None:
        self._store: dict[Any, tuple[Any, float]] = {}

    def set(self, key: Any, value: Any, ttl_seconds: float) -> None:
        expires_at = time.monotonic() + ttl_seconds
        self._store[key] = (value, expires_at)

    def get(self, key: Any) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() >= expires_at:
            # Lazy eviction
            self._store.pop(key, None)
            return None
        return value

    def __len__(self) -> int:
        return len(self._store)
