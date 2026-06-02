"""Per-build freshness collector — makes a view's TRUE freshness observable.

A view assembles several UW endpoints, each TTL-cached at a DIFFERENT age. One
build-time `fetched_at` hides that (a live spot next to a 4-min cached IV reads
as "fresh"). This collector lets `storage._through` report each served read's
observation time + provenance (live/cache/archive) into a per-build contextvar;
the build then stamps `as_of` = oldest field and `data_provenance` = worst case.

Mirrors the existing contextvar patterns (`storage._CACHED_ONLY`, `budget`).
No I/O, no storage import — storage imports THIS, so keep it dependency-free.
"""
from __future__ import annotations
import contextlib
import contextvars
from datetime import datetime
from typing import Literal

Provenance = Literal["live", "cache", "archive"]

# Worst-wins severity: a single archived field makes the whole view "archive".
_SEVERITY = {"live": 0, "cache": 1, "archive": 2}
_BY_SEVERITY = {v: k for k, v in _SEVERITY.items()}

# Each active build pushes a fresh list of (observed_at, provenance) records.
_COLLECTOR: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "freshness_collector", default=None)


class _Handle:
    """Returned by collect(); .summary() reduces the records gathered in-scope."""
    def __init__(self, records: list) -> None:
        self._records = records

    def summary(self) -> dict:
        return _summarize(self._records)


def _summarize(records: list) -> dict:
    n = {"live": 0, "cache": 0, "archive": 0}
    worst = 0
    oldest: datetime | None = None
    for observed_at, prov in records:
        n[prov] += 1
        worst = max(worst, _SEVERITY[prov])
        if observed_at is not None and (oldest is None or observed_at < oldest):
            oldest = observed_at
    return {
        "as_of": oldest.isoformat() if oldest is not None else None,
        "data_provenance": _BY_SEVERITY[worst],
        "n_live": n["live"], "n_cache": n["cache"], "n_archive": n["archive"],
    }


@contextlib.contextmanager
def collect():
    """Open a fresh per-build collection scope. record() calls within the block
    accumulate here; the yielded handle's summary() reduces them."""
    records: list = []
    token = _COLLECTOR.set(records)
    try:
        yield _Handle(records)
    finally:
        _COLLECTOR.reset(token)


def record(endpoint: str, observed_at: datetime | None, provenance: Provenance) -> None:
    """Append one served read's freshness to the active collector. No-op when no
    collector is active (so non-build calls — health checks, the loop — are
    unaffected). `endpoint` is accepted for future per-field breakdown; only
    observed_at + provenance feed the summary today."""
    records = _COLLECTOR.get()
    if records is None:
        return
    records.append((observed_at, provenance))


def current_summary() -> dict | None:
    """Summary of the active collector, or None if none active."""
    records = _COLLECTOR.get()
    if records is None:
        return None
    return _summarize(records)


def stamp(payload: dict) -> None:
    """Inject `as_of` + `data_provenance` into a view payload from the active
    collector. No-op outside a collector (leaves the dict untouched)."""
    s = current_summary()
    if s is None:
        return
    payload["as_of"] = s["as_of"]
    payload["data_provenance"] = s["data_provenance"]
