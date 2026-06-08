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

from pydantic import ValidationError

from server.models import FlowAlert
from server.pipeline.ingest import RawRecord
from server.services import provenance as prov

# flow-alerts pages at 500 (UW cap); len == cap ⇒ the session tail may be truncated.
_FLOW_ALERTS_CAP = 500


class NormalizeError(Exception):
    """A bronze payload whose SHAPE is irrecoverably wrong — missing required keys,
    wrong type on a required field, a sign-convention violation. Typed so the caller
    can degrade vs propagate, and so a real shape break never leaks as an untyped
    pydantic ValidationError three layers up."""


# endpoint-key -> parser. Keyed by the same slug Ingest uses for the bronze partition
# (`endpoint.strip('/').replace('/', '_')`), so dispatch can never miss.
REGISTRY: dict[str, Callable[[RawRecord], list]] = {}


def register(endpoint_key: str):
    def deco(fn: Callable[[RawRecord], list]):
        REGISTRY[endpoint_key] = fn
        return fn
    return deco


def _ep_key(endpoint: str) -> str:
    return endpoint.strip("/").replace("/", "_")


def normalize(raw: RawRecord) -> list:
    """Dispatch a raw record to its registered parser. Raises if no parser is
    registered (a missing parser is a wiring error, surfaced — not a silent skip)."""
    parser = REGISTRY.get(_ep_key(raw.endpoint))
    if parser is None:
        raise NotImplementedError(f"no normalizer registered for endpoint '{_ep_key(raw.endpoint)}'")
    return parser(raw)


def _unwrap(payload) -> list:
    """UW responses are {"data": [...]} or a bare list. Normalize to a list of rows.
    A structurally valid empty response ([]) is legitimate (e.g. pre-market, no alerts)
    — that is NOT an error; a wrong SHAPE is."""
    if isinstance(payload, dict):
        d = payload.get("data", payload)
        return d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
    return payload if isinstance(payload, list) else []


@register("option-trades_flow-alerts")
def normalize_flow_alerts(raw: RawRecord) -> list[FlowAlert]:
    """Raw flow-alerts payload → validated `list[FlowAlert]`. This is the detect-don't-
    trust boundary: every row is validated; a malformed row raises `NormalizeError`
    (never a silent null). `volume_oi_ratio` is REQUIRED here (Phase-2 confirmed it is
    always present and it is the opening-flow / direction input) — its absence is a loud
    failure. Truncation (`len == 500` cap) is flagged on each alert so Derive/Present can
    surface it honestly (flow-alerts-truncation lesson)."""
    rows = _unwrap(raw.payload)
    p = prov.archive(raw.fetched_at) if raw.from_replay else prov.live(raw.fetched_at)
    truncated = len(rows) >= _FLOW_ALERTS_CAP
    out: list[FlowAlert] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise NormalizeError(f"flow-alerts row {i} is not an object: {type(r).__name__}")
        try:
            fa = FlowAlert.model_validate({**r, "provenance": p, "truncated": truncated})
        except ValidationError as e:
            raise NormalizeError(f"flow-alerts row {i} failed validation: {e}") from e
        if fa.volume_oi_ratio is None:
            raise NormalizeError(
                f"flow-alerts row {i} missing volume_oi_ratio (the opening-flow direction "
                "input) — refusing to fabricate a direction from a row without it")
        out.append(fa)
    return out
