# Freshness Envelope (Atomic-Freshness Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stamp every on-demand view with `as_of` = its OLDEST contributing field's real pull time + `data_provenance` = worst-case source (live/cache/archive), and surface it per-tile, so the inevitable TTL-cache mixing becomes HONEST and visible instead of hidden behind one build-time timestamp.

**Architecture:** A process-local **freshness contextvar collector** (mirrors the existing `budget` + `storage._CACHED_ONLY` contextvar patterns). `storage._through` calls `freshness.record(...)` on every served read, tagging it live/cache/archive with its observation time. The on-demand routes wrap each build in `freshness.collect()`, then stamp the resulting payload via `freshness.summary()`. The frontend shows "as of HH:MM ET" per tile with a stale tint when the age exceeds the view's target. Per-endpoint TTL caching is KEPT (budget) — this makes mixing honest, not eliminated.

**Tech Stack:** Python 3.11, contextvars, pyarrow (existing parquet read), pydantic v2 (`Row` already `extra="allow"`), vanilla JS frontend (static/index.html). pytest.

**Spec:** `docs/superpowers/specs/2026-06-02-freshness-envelope-design.md`

---

## File Structure

- **Create** `server/freshness.py` — the collector: `collect()` contextmanager, `record()`, `summary()`, `stamp()`. One responsibility: accumulate per-read freshness within a build and summarize it. No I/O, no UW, no storage imports (avoid a cycle — storage imports it, not vice-versa).
- **Modify** `server/cache.py` — `TTLCache.set/get` carry an `observed_at` alongside the value, so a RAM hit reports the data's ORIGINAL pull time, not the cache-set time.
- **Modify** `server/storage.py` — `_read_latest_from_parquet` returns `(payload, fetched_at)`; `_through` calls `freshness.record(...)` on each served path (RAM / parquet-fresh / parquet-archive / live).
- **Modify** `server/main.py` — `_atomic_view` and the tile3/tile4/lookup routes wrap builds in `freshness.collect()` and stamp `as_of`/`data_provenance`.
- **Modify** `static/index.html` — per-tile "as of HH:MM ET" + stale tint, reusing the existing `fmtET` helper and `.stale` styling.
- **Tests:** `tests/test_freshness.py` (new), `tests/test_cache.py`, `tests/test_storage.py`, `tests/test_main.py`, `tests/test_html_preservation.py`.

---

## Task 1: Freshness collector (`server/freshness.py`)

**Files:**
- Create: `server/freshness.py`
- Test: `tests/test_freshness.py`

The collector is a contextvar holding a list of `(observed_at, provenance)` records.
`summary()` reduces them to `{as_of, data_provenance, n_live, n_cache, n_archive}`.
Provenance severity (worst wins): `archive` > `cache` > `live`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_freshness.py`:

```python
from datetime import datetime, timezone

from server import freshness


def _dt(minute):
    return datetime(2026, 6, 2, 14, minute, 0, tzinfo=timezone.utc)


def test_summary_outside_collector_is_empty():
    # record() is a no-op when no collector is active (non-build calls unaffected)
    freshness.record("greeks", _dt(0), "live")  # must not raise
    assert freshness.current_summary() is None


def test_collect_records_and_summarizes_min_and_worst():
    with freshness.collect() as fc:
        freshness.record("spot_exposures_strike", _dt(10), "live")
        freshness.record("greeks", _dt(5), "cache")      # older + worse
        freshness.record("atm_chains", _dt(8), "live")
        s = fc.summary()
    assert s["as_of"] == _dt(5).isoformat()       # oldest contributing field
    assert s["data_provenance"] == "cache"         # worst severity among reads
    assert s["n_live"] == 2 and s["n_cache"] == 1 and s["n_archive"] == 0


def test_archive_is_worst_provenance():
    with freshness.collect() as fc:
        freshness.record("a", _dt(10), "live")
        freshness.record("b", _dt(9), "cache")
        freshness.record("c", _dt(8), "archive")
        s = fc.summary()
    assert s["data_provenance"] == "archive"


def test_empty_collection_summarizes_to_none_as_of():
    with freshness.collect() as fc:
        s = fc.summary()
    assert s["as_of"] is None
    assert s["data_provenance"] == "live"   # neutral default when nothing recorded


def test_nested_collect_is_isolated():
    with freshness.collect() as outer:
        freshness.record("x", _dt(20), "live")
        with freshness.collect() as inner:
            freshness.record("y", _dt(1), "archive")
            inner_s = inner.summary()
        outer_s = outer.summary()
    assert inner_s["as_of"] == _dt(1).isoformat()
    assert inner_s["data_provenance"] == "archive"
    # inner did not leak into outer
    assert outer_s["as_of"] == _dt(20).isoformat()
    assert outer_s["data_provenance"] == "live"


def test_stamp_injects_keys_into_dict():
    payload = {"status": "ok", "ticker": "SPY"}
    with freshness.collect():
        freshness.record("greeks", _dt(7), "cache")
        freshness.stamp(payload)
    assert payload["as_of"] == _dt(7).isoformat()
    assert payload["data_provenance"] == "cache"


def test_stamp_outside_collector_is_noop():
    payload = {"status": "ok"}
    freshness.stamp(payload)   # no active collector
    assert "as_of" not in payload
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_freshness.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.freshness'` (or AttributeError once the file is stubbed).

- [ ] **Step 3: Implement `server/freshness.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_freshness.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add server/freshness.py tests/test_freshness.py
git commit -m "feat(freshness): per-build freshness collector (contextvar)"
```

---

## Task 2: TTLCache carries observed_at

**Files:**
- Modify: `server/cache.py`
- Test: `tests/test_cache.py`

A RAM cache hit must report the data's ORIGINAL pull time, not when it was
cached. Extend `set` to accept an optional `observed_at` and `get` to return
`(value, observed_at)`. Back-compat: callers that don't pass `observed_at` get
`None` back (storage will fall back to "now" — see Task 3).

NOTE: `get` changing its return shape from `value` to `(value, observed_at)` is a
breaking change for current callers. The only callers are `storage._through`
(updated in Task 3) and `insights` (5-min TTL on Gemini output). Task 2 updates
the insights caller too so nothing breaks between tasks.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cache.py`:

```python
from datetime import datetime, timezone
from server.cache import TTLCache


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
```

Any EXISTING test in `tests/test_cache.py` that asserts `c.get(k) == value`
must be updated to unpack the pair (`value, _ = c.get(k)`); do this in Step 3.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cache.py -q`
Expected: FAIL — `get()` returns a bare value / `set()` rejects the `observed_at` kwarg.

- [ ] **Step 3: Update `server/cache.py`**

Replace the `set` and `get` methods (and the `_store` type) with:

```python
    def __init__(self) -> None:
        # value, expires_at (monotonic), observed_at (wall-clock of original pull)
        self._store: dict[Any, tuple[Any, float, Any]] = {}

    def set(self, key: Any, value: Any, ttl_seconds: float, observed_at: Any = None) -> None:
        expires_at = time.monotonic() + ttl_seconds
        self._store[key] = (value, expires_at, observed_at)

    def get(self, key: Any) -> tuple[Any, Any]:
        """Return (value, observed_at). A miss/expiry returns (None, None)."""
        entry = self._store.get(key)
        if entry is None:
            return (None, None)
        value, expires_at, observed_at = entry
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)   # lazy eviction
            return (None, None)
        return (value, observed_at)
```

Then update the OTHER caller of `_cache.get` — `server/insights.py`. Find it:

Run: `python -m pytest -q 2>&1 | head -5` is not how to find it — instead:
`grep -rn "\.get(" server/insights.py` and locate the TTLCache `.get(...)` use.
Wherever insights does `cached = self._cache.get(key)` / `if cached is not None`,
change to:

```python
        cached, _ = self._cache.get(key)
        if cached is not None:
            return cached
```

- [ ] **Step 4: Run the full suite to verify nothing else broke**

Run: `python -m pytest tests/test_cache.py tests/test_insights.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/cache.py server/insights.py tests/test_cache.py
git commit -m "feat(cache): TTLCache carries observed_at; get returns (value, observed_at)"
```

---

## Task 3: `_through` records freshness per served read

**Files:**
- Modify: `server/storage.py` (`_read_latest_from_parquet` ~190-249, `_through` ~310-368)
- Test: `tests/test_storage.py`

`_read_latest_from_parquet` returns `(payload, fetched_at)` so `_through` knows
the served row's real observation time. `_through` records into the freshness
collector on each served path. Provenance: RAM/parquet-within-TTL → "cache";
parquet in cached_only/replay (TTL ignored, aged) → "archive"; live UW → "live".

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_storage.py` (uses a tmp DATA_DIR; follow the existing fixture
pattern in that file for `monkeypatch.setenv("DATA_DIR", ...)` — reuse whatever
helper the file already has to write a parquet row):

```python
from datetime import datetime, timezone
from server import storage, freshness


def test_through_records_live_provenance(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "_cache", storage.TTLCache())  # isolate RAM cache
    def _call():
        return {"data": [{"x": 1}]}
    with freshness.collect() as fc:
        out = storage._through("realized_vol", "SPY", None, False, _call)
        s = fc.summary()
    assert out == {"data": [{"x": 1}]}
    assert s["n_live"] == 1 and s["data_provenance"] == "live"
    assert s["as_of"] is not None   # stamped with ~now


def test_through_ram_hit_records_cache_with_original_observed_at(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "_cache", storage.TTLCache())
    obs = datetime(2026, 6, 2, 13, 0, tzinfo=timezone.utc)
    key = storage._make_key("realized_vol", "SPY", None)
    storage._cache.set(key, {"data": [1]}, ttl_seconds=60, observed_at=obs)
    with freshness.collect() as fc:
        out = storage._through("realized_vol", "SPY", None, False, lambda: None)
        s = fc.summary()
    assert out == {"data": [1]}
    assert s["data_provenance"] == "cache"
    assert s["as_of"] == obs.isoformat()   # ORIGINAL pull time, not cache-set time


def test_through_archive_provenance_in_cached_only(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "_cache", storage.TTLCache())
    # Write an AGED parquet row (2h old) then read it in cached_only (replay).
    old = datetime.now(tz=timezone.utc).replace(microsecond=0)
    from datetime import timedelta
    old = old - timedelta(hours=2)
    storage.write_response("realized_vol", "SPY", None, {"data": [9]},
                           status_code=200, latency_ms=1, fetched_at=old)
    with freshness.collect() as fc, storage.cached_only():
        out = storage._through("realized_vol", "SPY", None, False, lambda: None)
        s = fc.summary()
    assert out == {"data": [9]}
    assert s["data_provenance"] == "archive"
    # as_of reflects the aged parquet row, not now
    assert s["as_of"].startswith(old.isoformat()[:16])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_storage.py -k "records or provenance or archive" -q`
Expected: FAIL — `_through` doesn't record; `_read_latest_from_parquet` returns a bare payload so the tuple unpacking in `_through` (added in Step 3) isn't there yet.

- [ ] **Step 3: Update `server/storage.py`**

(a) Import freshness at the top (with the other `from server import ...`):

```python
from server import budget, freshness, uw
```

(b) `_read_latest_from_parquet` — return `(payload, fetched_at)`. Change the
return type and the two `return` sites. Replace the success `return` (currently
`return json.loads(response_str)`) with:

```python
                response_str = filtered["response"][latest_idx].as_py()
                fetched_at = filtered["fetched_at"][latest_idx].as_py()
                # pyarrow returns a tz-aware datetime for timestamp("us", tz="UTC")
                return json.loads(response_str), fetched_at
```

And the final `return None` (no row found) becomes:

```python
    return None, None
```

(c) `_through` — record on each served path. Replace the body from the RAM-cache
block through the live-call return with:

```python
    key = _make_key(endpoint, ticker, params)
    ttl = _ttl_seconds(endpoint, is_hot)

    # 1. RAM cache (fastest path)
    cached, cached_observed_at = _cache.get(key)
    if cached is not None:
        freshness.record(endpoint, cached_observed_at, "cache")
        return cached

    # 2. Parquet cache (persistent, survives restarts). In cached-only (replay)
    # mode we ignore the TTL — replay reads captured archives that are hours/days
    # old, and freshness is irrelevant when there's no live UW to fall back to.
    replay = _CACHED_ONLY.get()
    max_age = None if replay else ttl
    parquet_hit, parquet_fetched_at = _read_latest_from_parquet(
        endpoint, ticker, params, max_age_seconds=max_age)
    if parquet_hit is not None:
        _cache.set(key, parquet_hit, ttl_seconds=ttl, observed_at=parquet_fetched_at)
        # A within-TTL hit is "cache"; an aged hit served only because we're in
        # replay/cached-only is "archive" (honestly old).
        freshness.record(endpoint, parquet_fetched_at, "archive" if replay else "cache")
        return parquet_hit

    # 2a. Cached-only mode (request path): never call UW mid-request.
    if _CACHED_ONLY.get():
        return UWFailure(endpoint=endpoint, ticker=ticker,
                         message="cached-only: not yet warmed by the snapshot loop")

    # 2b. Soft budget guard (unchanged).
    if endpoint != "flow_alerts" and budget.over_soft_budget():
        return UWFailure(endpoint=endpoint, ticker=ticker,
                         message="budget guard: daily soft cap reached")

    # 3. UW (only when nothing fresh exists)
    started_at = time.monotonic()
    try:
        with _uw_call_gate:
            response = uw_call()
    except uw.UWError as e:
        return UWFailure(endpoint=endpoint, ticker=ticker, message=str(e))
    latency_ms = int((time.monotonic() - started_at) * 1000)
    now = datetime.now(tz=timezone.utc)
    write_response(
        endpoint=endpoint, ticker=ticker, params=params, response=response,
        status_code=200, latency_ms=latency_ms, fetched_at=now,
    )
    _cache.set(key, response, ttl_seconds=ttl, observed_at=now)
    freshness.record(endpoint, now, "live")
    return response
```

NOTE: a `UWFailure` is NOT recorded — a missing field shouldn't push `as_of`. The
view's `as_of` then reflects only the fields it actually got.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_storage.py -q`
Expected: PASS (existing storage tests still green — the only external behavior
change is the `_read_latest_from_parquet` return shape, used only inside `_through`).

- [ ] **Step 5: Commit**

```bash
git add server/storage.py tests/test_storage.py
git commit -m "feat(freshness): _through records per-read provenance + observed_at"
```

---

## Task 4: Stamp on-demand views (`as_of` + `data_provenance`)

**Files:**
- Modify: `server/main.py` (`_atomic_view` ~54-72, tile3/tile4/lookup routes ~185-281)
- Test: `tests/test_main.py`

Wrap each on-demand build in `freshness.collect()` and stamp the payload. tile3
and tile4 return dicts (stamp directly). lookup returns a `Row` (`extra="allow"`)
— stamp via the collector summary before `model_dump`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py` (these run against REPLAY/cached_only using the test
client already set up in that file — follow its existing fixture; if the file
seeds a snapshot+archive, reuse that. If a tile4/tile3 fixture ticker isn't
available offline, assert the KEYS exist rather than specific times):

```python
def test_tile4_view_carries_as_of_and_provenance(client_replay):
    # client_replay = TestClient with REPLAY=1 + DATA_DIR pointing at a fixture
    # archive that has the ticker warmed. Reuse the existing replay client helper.
    r = client_replay.get("/api/tile4/SPY")
    body = r.json()
    if body.get("status") == "ok":
        assert "as_of" in body
        assert body["data_provenance"] in ("live", "cache", "archive")


def test_tile3_view_carries_as_of(client_replay):
    r = client_replay.get("/api/tile3/SPY")
    body = r.json()
    if body.get("status") == "ok":
        assert "as_of" in body and "data_provenance" in body


def test_lookup_row_carries_as_of(client_replay):
    r = client_replay.get("/api/lookup/SPY")
    body = r.json()
    # build_single_row returns a full row; stamped via extra fields
    if "ticker" in body and "spot" in body:
        assert "as_of" in body
```

If `tests/test_main.py` has no `client_replay` fixture, add one mirroring the
existing client fixture but with `monkeypatch.setenv("REPLAY", "1")` and
`DATA_DIR` set to the committed fixture archive (look for how other replay-aware
tests in the repo set this up — `tests/test_main.py` or `conftest.py`). If no
offline archive fixture exists for SPY, mark these `@pytest.mark.skipif` on the
archive's absence so they're honest skips, not false greens.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_main.py -k "as_of or provenance" -q`
Expected: FAIL (`as_of` not in body) or honest SKIP if no offline archive.

- [ ] **Step 3: Update `server/main.py`**

(a) Import freshness:

```python
from server import budget, freshness, market_hours, storage
```
(add `freshness` to the existing server import line; keep the others).

(b) `_atomic_view` — wrap the build in a collection scope and stamp a fresh
success. The persisted last-good already carries its own `as_of` from when it was
built, so a stale replay shows the TRUE original moment (no change needed there):

```python
def _atomic_view(build_fn, view: str, ticker: str):
    """... (existing docstring) ..."""
    with freshness.collect():
        out = build_fn()
        if isinstance(out, dict) and out.get("status") in ("ok", "stand_down"):
            freshness.stamp(out)          # as_of = oldest field, provenance = worst
            storage.save_view(view, ticker, out)
            return out
    last_good = storage.read_view(view, ticker)
    if isinstance(last_good, dict) and last_good.get("status") in ("ok", "stand_down"):
        return {**last_good, "stale": True}
    return out
```

(c) The REPLAY branch of tile3/tile4 routes bypasses `_atomic_view`. Wrap those
too. In `tile3_detail_route._run`:

```python
    def _run():
        if _replay_enabled():
            with freshness.collect(), storage.cached_only():
                d = tile3_detail.build_tile3_detail(t, flow_spot, direction)
                if isinstance(d, dict) and d.get("status") in ("ok", "stand_down"):
                    freshness.stamp(d)
                return d
        return _atomic_view(
            lambda: tile3_detail.build_tile3_detail(t, flow_spot, direction), "tile3", t)
```

In `tile4_route._run`:

```python
    def _run():
        if _replay_enabled():
            with freshness.collect(), storage.cached_only():
                d = tile4.build_tile4(t, ctx)
                if isinstance(d, dict) and d.get("status") in ("ok", "stand_down"):
                    freshness.stamp(d)
                return d
        return _atomic_view(lambda: tile4.build_tile4(t, ctx), "tile4", t)
```

(d) `lookup_route` — `build_single_row` returns a `Row` (`extra="allow"`). Wrap
its build and inject the stamp as extra fields before dump. Replace the build
block:

```python
    t = ticker.upper()
    try:
        with freshness.collect() as fc:
            if _replay_enabled():
                with storage.cached_only():
                    row = await snapshot_mod.build_single_row(t)
            else:
                row = await snapshot_mod.build_single_row(t)
            fsum = fc.summary()
    except Exception as e:
        log.warning("lookup %s failed: %s", t, e)
        return JSONResponse({"status": "unavailable", "ticker": t, "reason": str(e)})
    if row is None:
        return JSONResponse({"status": "unavailable", "ticker": t,
                             "reason": "no data for this ticker"})
    payload = row.model_dump(mode="json")
    payload["as_of"] = fsum["as_of"]
    payload["data_provenance"] = fsum["data_provenance"]
    return JSONResponse(payload)
```

NOTE: `build_single_row` runs in the same thread/async context here, so the
contextvar set by `collect()` is visible to the storage calls it makes. (tile3/
tile4 builds run via `asyncio.to_thread`, but the `collect()` is INSIDE `_run`,
which is the thread target — so the contextvar is set in the same thread. Good.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_main.py -q`
Expected: PASS (or honest skips where no offline archive).

- [ ] **Step 5: Commit**

```bash
git add server/main.py tests/test_main.py
git commit -m "feat(freshness): stamp tile3/tile4/lookup views with as_of + provenance"
```

---

## Task 5: Frontend — per-tile "as of" + stale tint

**Files:**
- Modify: `static/index.html` (`renderTile4Picker` ~2936-3048, `renderTile3Rich` ~2289+, `fmtET` ~1789)
- Test: `tests/test_html_preservation.py`

Show "as of HH:MM ET" sourced from the view's `as_of`, tinted when older than the
view's freshness target (reuse `.stale` color). One small helper + a line in each
deep-dive tile header.

- [ ] **Step 1: Add a freshness-line helper near `fmtET`**

After the `fmtET` function (~line 1791), add:

```javascript
// Per-view freshness line: "as of 10:42 ET" + provenance, tinted stale when the
// view's oldest field exceeds `staleSec`. `asOf` is an ISO string from the view
// payload (oldest contributing field); `prov` ∈ live|cache|archive.
function freshnessLine(asOf, prov, staleSec) {
  if (!asOf) return "";
  const d = new Date(asOf);
  if (isNaN(d)) return "";
  const ageSec = (Date.now() - d.getTime()) / 1000;
  const stale = ageSec > (staleSec || 300);
  const provTag = prov && prov !== "live" ? ` · ${prov}` : "";
  const cls = stale ? "stale" : "";
  return `<span class="t-asof ${cls}" title="oldest contributing field${provTag}">as of ${fmtET(asOf)} ET${provTag}</span>`;
}
```

- [ ] **Step 2: Add the `.t-asof` style**

Near `.row .timestamp.stale` (~line 300), add:

```css
  .t-asof { font-size: 9px; color: var(--axis); font-family: var(--ff-mono); margin-left: 8px; }
  .t-asof.stale { color: var(--warn); }
```

- [ ] **Step 3: Render the line in Tile 4 header**

In `renderTile4Picker`, change the `<h3>` header (currently ending
`...data-help="tile4">?</button>${staleTag}`) to also include the freshness line.
Tile 4's target: 300s (its endpoints are MEDIUM-TTL). Replace the staleTag line region:

```javascript
  const asOfTag = freshnessLine(t4.as_of, t4.data_provenance, 300);
  return `
    <h3>
      <span><span class="gate-dot ${g}"></span>Tile 4: Contract Picker</span>
      <button class="help-icon" data-help="tile4">?</button>${staleTag}${asOfTag}
    </h3>
```

- [ ] **Step 4: Render the line in Tile 3 header**

In `renderTile3Rich`, find its `<h3>` header and append `${freshnessLine(detail.as_of, detail.data_provenance, 300)}` to it the same way. (Read the function first to match its exact header markup — it has a help-icon like Tile 4.)

- [ ] **Step 5: Verify offline + screenshot**

Run the replay server and screenshot a deep-dive to confirm the "as of" line
renders and tints. (See Task 6 for the exact commands — this step is the visual
check; do it as part of Task 6's verification.)

- [ ] **Step 6: Update HTML-preservation test if it trips**

Run: `python -m pytest tests/test_html_preservation.py -q`
If it fails on the sanctioned growth (per CLAUDE.md, the size/function-count
guards are FLOOR checks against deletion — adding markup may still trip a ceiling
if one remains), retune the specific assertion to match the new intent and keep
it meaningful (still guards accidental drift). Show the diff of the test change.

- [ ] **Step 7: Commit**

```bash
git add static/index.html tests/test_html_preservation.py
git commit -m "feat(freshness): per-tile 'as of' + stale tint (Tile 3/4 headers)"
```

---

## Task 6: Offline verification (replay) + screenshot

**Files:** none (verification only).

- [ ] **Step 1: Confirm an offline archive is present**

Run: `ls data/raw 2>/dev/null | head` — if empty, pull one:
`python scripts/pull_archive.py --token "$env:BACKFILL_TOKEN"` (needs the token;
if unavailable, use whatever fixture archive the test suite uses).

- [ ] **Step 2: Run the replay server (background)**

Run (background): `$env:DATA_DIR="./data"; $env:REPLAY="1"; .venv/Scripts/python -m uvicorn server.main:app --port 8000`

- [ ] **Step 3: Hit the views and confirm `as_of` is present**

Run: `Invoke-RestMethod http://localhost:8000/api/tile4/SPY | Select-Object status,as_of,data_provenance`
Expected: `status=ok`, `as_of` a non-null ISO string, `data_provenance` = `archive` (replay reads aged parquet → honestly "archive").
Repeat for `/api/tile3/SPY` and `/api/lookup/SPY`.

- [ ] **Step 4: Screenshot a deep-dive (Playwright)**

Navigate to `http://localhost:8000`, click SPY, open the deep-dive, and screenshot
Tiles 3 + 4. Confirm the "as of HH:MM ET · archive" line renders and is tinted
(replay data is old → stale tint expected). Use the Playwright MCP browser tools.

- [ ] **Step 5: Full suite green**

Run: `python -m pytest -q`
Expected: all pass (live-marked tests skip without UW_API_KEY). Confirm the count
is ≥ the pre-change baseline (234 passed) plus the new freshness tests.

- [ ] **Step 6: Final commit (if any verification fixes were needed)**

```bash
git add -A
git commit -m "test(freshness): offline replay verification of as_of envelope"
```

---

## Self-Review

**Spec coverage:**
- Mechanism (contextvar collector, severity, summary) → Task 1. ✓
- `_read_latest_from_parquet` returns fetched_at → Task 3 Step 3(b). ✓
- `_through` records live/cache/archive → Task 3 Step 3(c). ✓
- TTLCache carries observed_at (RAM hit reports original time) → Task 2. ✓
- Builds stamp `as_of` + `data_provenance` (tile3/tile4/lookup) → Task 4. ✓
- Atomic replay carries persisted as_of (no extra work — stamped at save) → Task 4 note. ✓
- Frontend per-view "as of" + stale tint → Task 5. ✓
- Offline verification + screenshot → Task 6. ✓
- Snapshot per-row as_of: spec lists it but the operator's chosen display is
  per-VIEW (deep-dive tiles); the snapshot grid already shows a global age line.
  **Scoped OUT of this plan** to keep it focused — lookup (the on-demand row) IS
  stamped (Task 4d); the background-loop snapshot grid keeps its existing global
  `fetched_at` age. A follow-up can stamp per-row as_of if wanted. Noted here so
  it's a conscious cut, not a gap.

**Placeholder scan:** No TBD/TODO. Every code step shows complete code. The one
"find the exact header markup" instruction (Task 5 Step 4) is a read-then-match,
not a placeholder — the pattern to copy is given explicitly from Tile 4.

**Type consistency:** `collect()` → `_Handle.summary()` → dict with `as_of`,
`data_provenance`, `n_live/n_cache/n_archive`; `record(endpoint, observed_at,
provenance)`; `stamp(payload)`; `current_summary()`. `TTLCache.get` → `(value,
observed_at)` everywhere (storage + insights updated in Task 2). `_read_latest_
from_parquet` → `(payload, fetched_at)`, unpacked only in `_through`. Consistent
across tasks.
