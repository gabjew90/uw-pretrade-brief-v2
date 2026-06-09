"""Provenance constructors — convenience stamps over the `Provenance` type (defined
in `server.models`). Keeping the type in models (so it can be a field on every entity)
and the *helpers* here keeps the boundary clean."""
from __future__ import annotations

from server.models import Provenance, Quality, Source


def live(as_of: str | None = None) -> Provenance:
    return Provenance(source=Source.LIVE, quality=Quality.REAL, as_of=as_of)


def cache(as_of: str | None = None) -> Provenance:
    return Provenance(source=Source.CACHE, quality=Quality.REAL, as_of=as_of)


def archive(as_of: str | None = None, *, degraded: bool = False) -> Provenance:
    return Provenance(source=Source.ARCHIVE,
                      quality=Quality.DEGRADED if degraded else Quality.REAL, as_of=as_of)


def derived(*inputs: Provenance) -> Provenance:
    """Worst-case provenance for a value computed from others (Derive stage). Keeps the
    worst-case UPSTREAM source (live/cache/archive) rather than collapsing to 'derived', so
    a tile can honestly answer "where did it come from?" — live UW vs settled/archived data —
    not just "computed"."""
    return Provenance.worst(*inputs)


def unavailable(note: str = "") -> Provenance:
    """For a value we could NOT obtain. Never paired with a fabricated value."""
    return Provenance(quality=Quality.UNAVAILABLE, note=note)
