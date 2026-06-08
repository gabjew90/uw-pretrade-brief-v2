"""Unusual Whales REST client — the ONLY place that touches the UW network.

Discipline carried from v2 (these bugs were invisible):
  * UW paths are HYPHENATED (flow-alerts, spot-exposures, historical-risk-reversal-skew).
    An underscore 404s silently. `assert_hyphenated()` lints callers; CI should too.
  * Every call is gated by the governor (budget/priority) FIRST — in REPLAY the
    governor denies, and Ingest must read bronze instead.
  * 429 → exponential backoff with jitter; persistent failure returns a typed UWError
    (caller records provenance.quality=unavailable, never a fabricated value).

Kept sync (requests) and wrapped in a thread pool by callers for parallelism — same as
v2. Do NOT add httpx/async here; the binding constraint is budget, not concurrency.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from server.config import settings
from server.services.governor import Decision, Priority, governor

_BASE = "https://api.unusualwhales.com/api"
_TIMEOUT = 20


class UWError(Exception):
    """A UW fetch that could not be completed (over budget, 4xx/5xx, or network)."""


@dataclass
class UWResponse:
    endpoint: str
    params: dict
    status: int
    json: dict
    fetched_at: str   # ISO-8601 UTC


def assert_hyphenated(path: str) -> None:
    """Guard: UW paths use hyphens, never underscores in the resource segments."""
    bad = [seg for seg in path.split("/") if "_" in seg]
    if bad:
        raise ValueError(f"UW path has underscores (will 404 silently): {bad} in {path!r} "
                         "— UW paths are HYPHENATED.")


def get(path: str, params: dict | None = None, *, priority: Priority = Priority.NORMAL,
        max_retries: int = 4) -> UWResponse:
    """GET an absolute UW API path (e.g. '/option-trades/flow-alerts'). Governor-gated;
    429-backoff. Raises UWError on denial/failure — never returns fabricated data."""
    assert_hyphenated(path)
    decision: Decision = governor.check(priority=priority)
    if not decision.allow:
        raise UWError(f"governor denied: {decision.reason}")
    if not settings.uw_api_key:
        raise UWError("no UW_API_KEY set")

    url = f"{_BASE}{path}"
    headers = {"Authorization": f"Bearer {settings.uw_api_key}", "Accept": "application/json"}
    delay = 0.6
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params or {}, headers=headers, timeout=_TIMEOUT)
        except requests.RequestException as e:
            if attempt == max_retries - 1:
                raise UWError(f"network: {e}") from e
            time.sleep(delay); delay *= 2; continue
        governor.record(1)
        if r.status_code == 429:
            if attempt == max_retries - 1:
                raise UWError("429 after retries")
            time.sleep(delay); delay *= 2; continue
        if r.status_code >= 400:
            raise UWError(f"HTTP {r.status_code} for {path}")
        from datetime import datetime, timezone
        return UWResponse(endpoint=path, params=params or {}, status=r.status_code,
                          json=r.json(), fetched_at=datetime.now(timezone.utc).isoformat())
    raise UWError("exhausted retries")
