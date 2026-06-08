# Provenance — Design (v3, Phase 1)

**Status:** PLAN (awaiting approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** `server.models` (type lives there) · **Starting point:** `server/services/provenance.py`

## Purpose / role in the pipeline

Subsumes v2's three scattered concepts — `is_synthetic`, honest-degrade flags, and
the freshness collector (contextvar + `data_provenance` stamps) — into one uniform
idea: **every value carries a `Provenance`**. The type is declared in `server.models`;
this module provides the stamping helpers and the worst-case merge so stages never
invent their own freshness logic. The frontend tints by `quality`; the operator sees
exactly why a signal looks the way it does.

## Contract (typed in/out — reference server.models + the contracts spec)

```python
# Type (server.models) — one source of truth, no copies
class Source(str, Enum): LIVE = "live"; CACHE = "cache"; ARCHIVE = "archive"
                         DERIVED = "derived"; UNAVAILABLE = "unavailable"
class Quality(str, Enum): REAL = "real"; DEGRADED = "degraded"; UNAVAILABLE = "unavailable"

class Provenance(BaseModel):
    source:  Source
    quality: Quality
    as_of:   str | None = None   # ISO-8601 UTC
    note:    str = ""
    @classmethod
    def worst(cls, *ps: "Provenance") -> "Provenance": ...

# Helpers (this module — pure constructors, no I/O)
live(as_of)          -> Provenance   # source=LIVE,    quality=REAL
cache(as_of)         -> Provenance   # source=CACHE,   quality=REAL
archive(as_of, *, degraded=False) -> Provenance
derived(*inputs)     -> Provenance   # source=DERIVED, quality=worst(inputs).quality
unavailable(note)    -> Provenance   # quality=UNAVAILABLE — never paired with a fabricated value
```

## Responsibilities (and explicit NON-responsibilities)

**Owns:**
- Convenience constructors (`live`, `cache`, `archive`, `derived`, `unavailable`).
- `Provenance.worst(*ps)`: worst quality wins (UNAVAILABLE > DEGRADED > REAL);
  worst source wins (UNAVAILABLE > ARCHIVE > CACHE > LIVE, then DERIVED below LIVE);
  oldest `as_of` wins (the view is only as fresh as its oldest field).
- `derived()` sets `source=DERIVED` and propagates the worst quality from its inputs —
  a Derive-stage signal computed from an ARCHIVE feed inherits ARCHIVE quality.

**Does NOT own:**
- The `Provenance` *type* (lives in `server.models`; never duplicated here).
- Any I/O, network call, or storage read.
- Frontend tint thresholds — those are render-layer decisions the operator controls.
- The contextvar pattern from v2's `freshness.py` — v3 attaches `Provenance` directly
  to typed model fields; no separate context collector is needed.

## Key behaviors / edge cases

- `worst()` with zero inputs → `unavailable("")` (safe default, not REAL).
- `unavailable` must never be paired with a fabricated value: `value=None` or the field
  absent. If a caller passes a value alongside `quality=UNAVAILABLE`, the Present stage
  must refuse to surface it.
- `derived(*inputs)` does NOT upgrade quality: if all inputs are REAL, derived is REAL;
  if any is UNAVAILABLE, derived is UNAVAILABLE. The intent is monotonic degradation.
- `as_of` comparison: ISO-8601 strings sort lexicographically only when in UTC with
  consistent format. Helpers always emit UTC ISO-8601 (`datetime.now(timezone.utc).isoformat()`).
- `archive(degraded=True)` → `quality=DEGRADED` for data that is present but stale
  beyond the operator's threshold. `archive(degraded=False)` → `quality=REAL` (fresh
  archive read within TTL).

## Keepers to port from v2 (`git show e1d6c5e:server/freshness.py`)

| v2 item | Where it lands in v3 |
|---|---|
| `_SEVERITY = {live:0, cache:1, archive:2}` worst-case logic | `Provenance.worst()` in `server.models` — same monotone ranking |
| `_COLLECTOR` contextvar + `collect()` / `record()` / `stamp()` | **Removed**: v3 attaches `Provenance` directly to model fields; no implicit collection needed |
| `data_provenance` worst-case string on the snapshot payload | Replaced by `ViewModel.provenance` (a `Provenance` object, not a string) |
| `as_of` = oldest field timestamp | `Provenance.worst(*inputs).as_of` in `derived()` |

The contextvar pattern was a workaround for v2's untyped dicts. v3's typed model
fields make it unnecessary.

## Acceptance criteria

- [ ] `Provenance.worst(live(t1), cache(t2), archive(t3))` → `source=ARCHIVE, quality=REAL, as_of=t3` (oldest wins).
- [ ] `Provenance.worst(live(t1), unavailable())` → `quality=UNAVAILABLE`.
- [ ] `Provenance.worst()` (empty) → `quality=UNAVAILABLE`.
- [ ] `derived(live(t1), archive(t2))` → `source=DERIVED, quality=REAL, as_of=t2`.
- [ ] `derived(live(t1), unavailable())` → `quality=UNAVAILABLE`.
- [ ] `archive(degraded=True).quality` → `DEGRADED`.
- [ ] `unavailable("no IV data").note` → `"no IV data"`.
- [ ] `Provenance` is importable from `server.models` with zero I/O side effects.
- [ ] All five helper constructors are importable from `server.services.provenance`.

## Definition of done

Typed in/out · provenance on every value (this module IS the provenance layer) ·
no boundary skipped · REPLAY-reproducible (pure functions, no time dependency).

## Defers to operator

- Frontend tint thresholds (which quality levels produce which visual treatments).
- Whether `DEGRADED` triggers a warning banner vs a subtle colour shift.
- Whether the `note` field is surfaced to the user or only used for operator debugging.

## Open questions / flags

- `source=UNAVAILABLE` vs `source=DERIVED` for a computed value whose inputs are all
  unavailable: current design uses `source=DERIVED, quality=UNAVAILABLE`. Alternatively
  use a dedicated `source=UNAVAILABLE`. Recommend: keep DERIVED for the source when
  the derivation ran but produced nothing — it distinguishes "we tried" from "we never
  fetched".
- `as_of` as a string (`str | None`) vs `datetime | None`: strings cross JSON
  serialization cleanly but lose ordering guarantees unless all callers emit UTC ISO-8601.
  Enforce in a validator if this bites.
