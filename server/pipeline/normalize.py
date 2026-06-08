"""Stage 2 — NORMALIZE → canonical typed records.

Parse a `RawRecord` (bronze) into validated pydantic models. ALL field-shape
validation happens HERE, once. If UW changes a key, flips a sign convention, or
truncates a feed, this boundary raises/flags explicitly — never a silent null three
layers downstream (v2's whole "invisible data bug" class).

Each endpoint gets a `normalize_<endpoint>(raw) -> list[CanonicalModel]` function
registered below. Canonical models (FlowAlert, OISnapshot, GreekFlowSeries, …) define
the EXACT fields the Derive stage may read — nothing reads bronze directly except here.

Stub: the canonical models + per-endpoint parsers are defined per operator
instructions. The registry + contract are fixed now.
"""
from __future__ import annotations

from typing import Callable

from server.pipeline.ingest import RawRecord

# endpoint-key -> parser. Filled per instructions (e.g. "flow-alerts": _flow_alerts).
REGISTRY: dict[str, Callable[[RawRecord], list]] = {}


def register(endpoint_key: str):
    def deco(fn: Callable[[RawRecord], list]):
        REGISTRY[endpoint_key] = fn
        return fn
    return deco


def normalize(raw: RawRecord) -> list:
    """Dispatch a raw record to its registered parser. Raises if no parser is
    registered (a missing parser is a wiring error, surfaced — not a silent skip)."""
    key = raw.endpoint.strip("/").replace("/", "_")
    parser = REGISTRY.get(key)
    if parser is None:
        raise NotImplementedError(f"no normalizer registered for endpoint '{key}'")
    return parser(raw)
