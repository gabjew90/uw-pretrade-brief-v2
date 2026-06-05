# Basic Platform — Lazy Request-Built Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the always-on 120s background ingestion loop with a request-driven dashboard: UW calls happen on page-load (a light build) and ticker-click (a full build), backed by a read-through cache in front of append-only immutable logs.

**Architecture:** Two append-only stores (the `snapshots.jsonl` log, canonical for the grid; the raw per-endpoint parquet archive, kept for deep-dive replay + cross-restart cache) sit behind per-namespace TTLs. `/` and `/snapshot.json` call a cache-or-build front door (`get_or_build_snapshot`) that serves the last build when fresh, else builds a light flow-only grid (1 `flow_alerts` call) and computes/carries-forward the regime on its own TTL. Clicks build full rows on demand via the existing `build_single_row`/lookup path.

**Tech Stack:** Python 3.11, FastAPI, pyarrow (parquet), pydantic v2, pytest (+responses/mock/asyncio), vanilla JS in `static/index.html`.

**Spec:** `docs/superpowers/specs/2026-06-05-basic-platform-design.md`

**Naming note (spec correction):** the spec calls the parquet writer `_append_row`; the real function is **`storage.write_response`** (`server/storage.py:83`). This plan uses the real name.

**Task order rationale:** storage safety first (append-only write + the read fix it forces) because concurrent light-build-vs-click-build depends on it; then schema; then the build functions; then routes; then loop removal; then frontend; then verify. Tasks 1–3 each ship working, independently-valuable software (they fix the write-path bug and harden the log even before the loop is removed).

---

### Task 1: Append-only parquet writes (fixes the read-modify-write bug)

**Files:**
- Modify: `server/storage.py` — `_partition_path` (`60-68`) and `write_response` (`83-115`)
- Test: `tests/test_storage_append_only.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_storage_append_only.py
import datetime as dt
import importlib


def _fresh_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from server import storage
    return importlib.reload(storage)


def test_concurrent_writes_same_hour_no_lost_update(tmp_path, monkeypatch):
    storage = _fresh_storage(tmp_path, monkeypatch)
    at = dt.datetime(2026, 6, 5, 14, 0, 0, tzinfo=dt.timezone.utc)
    # Two writes to the SAME endpoint/ticker/hour in the same second.
    assert storage.write_response("oi_per_strike", "AAPL", {}, {"data": [{"strike": 100, "call_oi": 1, "put_oi": 0}]}, 200, 5, at)
    assert storage.write_response("oi_per_strike", "AAPL", {}, {"data": [{"strike": 101, "call_oi": 2, "put_oi": 0}]}, 200, 5, at)
    d = tmp_path / "raw" / "endpoint=oi_per_strike" / "dt=2026-06-05" / "ticker=AAPL"
    parts = list(d.glob("part-*.parquet"))
    assert len(parts) == 2, "both writes must persist as separate part files (no lost update)"
    assert not list(d.glob("*.tmp")), "no temp file left behind (atomic temp→replace)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_storage_append_only.py -v`
Expected: FAIL — current `write_response` rewrites one `part-HHMM.parquet` in place, so the second write overwrites/concats into the same file → only 1 part file.

- [ ] **Step 3: Add the unique-filename helper and rewrite the writer**

In `server/storage.py`, add `import uuid` near the top imports (with the other stdlib imports). Add a directory helper next to `_partition_path`:

```python
def _partition_dir(endpoint: str, ticker: str | None, fetched_at: datetime) -> Path:
    """Directory for this (endpoint, ticker, day). One immutable part file per
    write lives here (see write_response)."""
    dt_str = fetched_at.strftime("%Y-%m-%d")
    parts = [_data_dir(), "raw", f"endpoint={endpoint}", f"dt={dt_str}"]
    if ticker:
        parts.append(f"ticker={ticker}")
    return Path(*parts)
```

Replace the body of `write_response` (the `try:` block that builds `path`, reads existing, concats, and `pq.write_table`) with an append-only atomic write — no read-modify-write:

```python
    try:
        d = _partition_dir(endpoint, ticker, fetched_at)
        d.mkdir(parents=True, exist_ok=True)
        row = {
            "fetched_at": [fetched_at],
            "params_json": [json.dumps(params or {}, sort_keys=True)],
            "status_code": [status_code],
            "latency_ms": [latency_ms],
            "response": [json.dumps(response, default=str)],
        }
        new_table = pa.table(row, schema=_record_schema())
        # One immutable part file per write, named so it sorts newest-last within
        # the day; uuid suffix avoids collisions for same-second concurrent writes.
        fname = f"part-{fetched_at.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}.parquet"
        path = d / fname
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(new_table, tmp, compression="zstd", use_dictionary=False)
        os.replace(tmp, path)   # atomic publish
        return True
    except Exception as e:
        log.error("storage write failed: endpoint=%s ticker=%s err=%s", endpoint, ticker, e)
        return False
```

Delete the now-unused `_partition_path` function (readers use `glob("part-*.parquet")`, never `_partition_path`; confirm with a grep before deleting).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_storage_append_only.py -v`
Expected: PASS (2 part files, no temp).

- [ ] **Step 5: Confirm nothing referenced `_partition_path`**

Run: `.venv/Scripts/python -m pytest -q`
Expected: full suite passes (no import error from the deletion). If any module imported `_partition_path`, update it to `_partition_dir`.

- [ ] **Step 6: Commit**

```bash
git add server/storage.py tests/test_storage_append_only.py
git commit -m "fix(storage): append-only atomic parquet writes (one immutable part file per write)"
```

---

### Task 2: `read_oi_history` correct across fragmented part files (correctness-critical)

The append-only change turns one part file/hour into many tiny part files. The current reader (`server/storage.py:254-309`) iterates files newest-first, applies the `{}`-param preference *within a single file*, and `break`s on the first file with strikes. With one row per file, the newest file may be a non-`{}` (date-param) write, which would shortcut selection and yield a wrong session bar. Fix: for each day, scan **all** part files, prefer `{}`-param rows by latest `fetched_at` across files.

**Files:**
- Modify: `server/storage.py` — `read_oi_history` (`254-309`)
- Test: `tests/test_storage_oi_history.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_storage_oi_history.py
import datetime as dt
import importlib


def _fresh_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from server import storage
    return importlib.reload(storage)


def _payload(strike, call_oi, put_oi):
    return {"data": [{"strike": strike, "call_oi": call_oi, "put_oi": put_oi}]}


def test_oi_history_aggregates_one_bar_per_session_across_many_part_files(tmp_path, monkeypatch):
    storage = _fresh_storage(tmp_path, monkeypatch)
    # Day 1: two intraday {} writes — the LATER one (14:00) is the session bar.
    storage.write_response("oi_per_strike", "AAPL", {}, _payload(100, 5, 0), 200, 5,
                           dt.datetime(2026, 6, 3, 13, 0, 0, tzinfo=dt.timezone.utc))
    storage.write_response("oi_per_strike", "AAPL", {}, _payload(100, 9, 0), 200, 5,
                           dt.datetime(2026, 6, 3, 14, 0, 0, tzinfo=dt.timezone.utc))
    # Day 1 also has a LATER date-param (backfill-style) write that must NOT win
    # over the {} live snapshot for the session bar.
    storage.write_response("oi_per_strike", "AAPL", {"date": "2026-06-03"}, _payload(100, 999, 0), 200, 5,
                           dt.datetime(2026, 6, 3, 15, 0, 0, tzinfo=dt.timezone.utc))
    # Day 2: single write.
    storage.write_response("oi_per_strike", "AAPL", {}, _payload(100, 7, 0), 200, 5,
                           dt.datetime(2026, 6, 4, 14, 0, 0, tzinfo=dt.timezone.utc))

    out = storage.read_oi_history("AAPL", 5)
    # Oldest → newest, one bar per session.
    assert [s["date"] for s in out] == ["2026-06-03", "2026-06-04"]
    # Day 1 picks the latest {} write (9), NOT the earlier {} (5) and NOT the date-param 999.
    assert out[0]["strikes"][100.0] == 9
    assert out[1]["strikes"][100.0] == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_storage_oi_history.py -v`
Expected: FAIL — the current reader breaks on the newest file (the 15:00 date-param write) and returns 999 for day 1.

- [ ] **Step 3: Rewrite the per-day selection**

Replace the per-day loop body in `read_oi_history` (the `for d in dt_dirs:` block) with a scan across all part files that prefers `{}`-param rows by latest `fetched_at`:

```python
    sessions: list[dict] = []
    for d in dt_dirs:
        date_str = d.name.replace("dt=", "")
        tdir = d / f"ticker={ticker}"
        best_ts = best_resp = None        # latest {}-param row for the day
        fb_ts = fb_resp = None            # fallback: latest any-param row
        for path in tdir.glob("part-*.parquet"):
            try:
                table = pq.read_table(path)
            except Exception as e:
                log.warning("oi-history read failed %s @ %s: %s", ticker, path.name, e)
                continue
            for i in range(table.num_rows):
                ts = table["fetched_at"][i].as_py()
                params = table["params_json"][i].as_py()
                resp = table["response"][i].as_py()
                if params == "{}":
                    if best_ts is None or ts > best_ts:
                        best_ts, best_resp = ts, resp
                else:
                    if fb_ts is None or ts > fb_ts:
                        fb_ts, fb_resp = ts, resp
        chosen = best_resp if best_resp is not None else fb_resp
        if chosen is None:
            continue
        try:
            payload = json.loads(chosen)
        except (TypeError, ValueError):
            continue
        rows = payload.get("data") if isinstance(payload, dict) else payload
        strikes: dict[float, int] = {}
        for r in (rows or []):
            try:
                k = float(r.get("strike") or 0)
                oi = int(r.get("call_oi") or 0) + int(r.get("put_oi") or 0)
                if k > 0:
                    strikes[k] = oi
            except (TypeError, ValueError):
                continue
        if strikes:
            sessions.append({"date": date_str, "strikes": strikes})
    sessions.reverse()  # oldest → newest
    return sessions
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_storage_oi_history.py -v`
Expected: PASS.

- [ ] **Step 5: Run the existing Tile 2 / storage suite for regressions**

Run: `.venv/Scripts/python -m pytest -q -k "oi_history or tile2 or storage"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/storage.py tests/test_storage_oi_history.py
git commit -m "fix(storage): read_oi_history selects one bar/session across fragmented part files"
```

---

### Task 3: Snapshot-log tail-seed + size cap

`read_last_snapshot` (`server/storage.py:657-683`) parses the whole `snapshots.jsonl` on every cold boot; the file grows unbounded. Read the tail instead (with a full-scan fallback), and cap the file on append.

**Files:**
- Modify: `server/storage.py` — `append_snapshot` (`611-621`), `read_last_snapshot` (`657-683`)
- Test: `tests/test_storage_snapshot_log.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_storage_snapshot_log.py
import importlib


def _fresh_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from server import storage
    return importlib.reload(storage)


def test_read_last_snapshot_returns_latest_good_via_tail(tmp_path, monkeypatch):
    storage = _fresh_storage(tmp_path, monkeypatch)
    storage.append_snapshot({"fetched_at": "2026-06-05T10:00:00Z", "rows": [{"ticker": "AAA"}]})
    storage.append_snapshot({"fetched_at": "2026-06-05T10:02:00Z", "rows": []})            # empty: skipped
    storage.append_snapshot({"fetched_at": "2026-06-05T10:04:00Z", "rows": [{"ticker": "BBB"}]})
    out = storage.read_last_snapshot()
    assert out is not None and out["rows"][0]["ticker"] == "BBB"


def test_append_snapshot_caps_file_lines(tmp_path, monkeypatch):
    storage = _fresh_storage(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "_MAX_SNAPSHOT_LINES", 10, raising=False)
    for i in range(25):
        storage.append_snapshot({"fetched_at": f"2026-06-05T10:{i:02d}:00Z", "rows": [{"ticker": f"T{i}"}]})
    lines = [ln for ln in storage._snapshots_path().read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) <= 10, "log must be capped"
    out = storage.read_last_snapshot()
    assert out["rows"][0]["ticker"] == "T24", "newest survives the cap"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_storage_snapshot_log.py -v`
Expected: FAIL — `_MAX_SNAPSHOT_LINES` doesn't exist and the file isn't capped (second test); first test may pass already but keep it as a guard.

- [ ] **Step 3: Add the cap + tail read**

In `server/storage.py`, add near the other module constants:

```python
_MAX_SNAPSHOT_LINES = 1000   # cap snapshots.jsonl; request-driven volume is low
```

Replace `append_snapshot` with a version that trims when over the cap (bounded, infrequent rewrite):

```python
def append_snapshot(snapshot: dict) -> bool:
    """Append one snapshot as a JSON line; trim to the last _MAX_SNAPSHOT_LINES.
    Best-effort I/O."""
    try:
        path = _snapshots_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, default=str) + "\n")
        # Opportunistic cap: only rewrites when over the bound.
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > _MAX_SNAPSHOT_LINES:
            keep = lines[-_MAX_SNAPSHOT_LINES:]
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(keep), encoding="utf-8")
            os.replace(tmp, path)
        return True
    except Exception as e:
        log.error("snapshot append failed: %s", e)
        return False
```

Replace `read_last_snapshot` with a tail-first reader that falls back to a full scan:

```python
def read_last_snapshot() -> dict | None:
    """Return the most recent persisted snapshot that HAS rows, as a dict. Reads
    the file TAIL (last 64 KB) first so cold-boot doesn't parse the whole log;
    falls back to a full scan if the tail holds no complete good line. Best-effort
    — never raises."""
    path = _snapshots_path()
    if not path.exists():
        return None

    def _scan(lines):
        good = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                snap = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(snap, dict) and snap.get("rows"):
                good = snap
        return good

    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="ignore")
        # Drop a possibly-partial first line when we didn't start at byte 0.
        tail_lines = tail.splitlines()
        hit = _scan(tail_lines[1:] if size > 65536 else tail_lines)
        if hit is not None:
            return hit
        # Fallback: the last good snapshot is older than the tail window.
        with path.open("r", encoding="utf-8") as f:
            return _scan(f.readlines())
    except Exception as e:
        log.warning("read_last_snapshot failed: %s", e)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_storage_snapshot_log.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/storage.py tests/test_storage_snapshot_log.py
git commit -m "perf(storage): tail-seed read_last_snapshot + cap snapshots.jsonl"
```

---

### Task 4: `Row.is_light` schema flag

**Files:**
- Modify: `server/schema.py` — `Row` model
- Test: `tests/test_schema.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema.py  (append)
def test_row_is_light_defaults_false():
    from server.schema import Row
    r = Row(ticker="AAPL")
    assert r.is_light is False
```

(If `Row(ticker=...)` requires more fields, mirror an existing `Row(...)` construction in this test file instead; the only assertion that matters is `is_light is False` by default.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_schema.py::test_row_is_light_defaults_false -v`
Expected: FAIL — `Row` has no `is_light`.

- [ ] **Step 3: Add the field**

In `server/schema.py`, add to the `Row` model (next to `is_synthetic`):

```python
    is_light: bool = False   # True = flow-only grid row (no per-ticker gamma/OI/cost);
                             # full gates + tiles fill in on click. See basic-platform spec.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_schema.py::test_row_is_light_defaults_false -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/schema.py tests/test_schema.py
git commit -m "feat(schema): Row.is_light flag for flow-only grid rows"
```

---

### Task 5: `build_light_snapshot()` — flow-only grid, no per-ticker heavy calls

**Files:**
- Modify: `server/snapshot.py` — add `build_light_snapshot` (near `refresh_snapshot`/`build_single_row`); reuse `universe.top_15_unique_tickers`, `_aggregate_flow_per_ticker`, `gates.compute_gates`, `gates.derive_direction`, `_project_flow_alerts`
- Test: `tests/test_light_snapshot.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_light_snapshot.py
import pytest
from server import snapshot as snap_mod


@pytest.mark.asyncio
async def test_build_light_snapshot_flow_only(monkeypatch):
    # One flow_alerts payload covering 2 tickers; assert NO per-ticker heavy fetch.
    flow_payload = {"data": [
        {"ticker": "AAA", "type": "call", "total_premium": 900000, "all_opening_trades": True, "underlying_price": 100},
        {"ticker": "BBB", "type": "put", "total_premium": 500000, "all_opening_trades": True, "underlying_price": 50},
    ]}
    monkeypatch.setattr(snap_mod.storage, "fetch_flow_alerts", lambda *a, **k: flow_payload)

    heavy_called = []
    for name in ("fetch_spot_exposures_strike", "fetch_oi_strike", "fetch_volatility",
                 "fetch_interpolated_iv", "fetch_darkpool"):
        monkeypatch.setattr(snap_mod.storage, name,
                            lambda *a, _n=name, **k: heavy_called.append(_n))
    # Regime is attached by the front door, not here — stub it out.
    monkeypatch.setattr(snap_mod, "_build_market_regime",
                        lambda loop: _async_none())

    snap = await snap_mod.build_light_snapshot()
    assert heavy_called == [], f"light build must not fetch per-ticker endpoints: {heavy_called}"
    assert len(snap.rows) >= 1
    for r in snap.rows:
        assert r.is_light is True
        assert r.gates["flow"] in ("green", "yellow", "red")


async def _async_none():
    return (None, False)
```

(If `build_light_snapshot` attaches the regime internally instead, adjust the stub; the binding assertions are: no heavy fetches, all rows `is_light`, flow gate set.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_light_snapshot.py -v`
Expected: FAIL — `build_light_snapshot` doesn't exist.

- [ ] **Step 3: Implement `build_light_snapshot`**

Add to `server/snapshot.py`:

```python
def _light_row(ticker: str, flow_info: dict) -> Row:
    """A flow-only grid row: flow gate + provisional direction, no per-ticker
    gamma/OI/IV. The other three gates are neutral ('gray'); the deep-dive fills
    them on click (build_single_row)."""
    flow_alerts_detail = _project_flow_alerts(flow_info.get("raw_alerts", []))
    # Direction from flow only. derive_direction's gamma leg needs per-ticker GEX
    # we don't fetch — so accept opening_flow/total_flow, but treat a gamma_fallback
    # result as "unknown until click" rather than a fabricated read.
    direction, basis = gates.derive_direction(flow_alerts_detail, "", 0.0)
    if basis == "gamma_fallback":
        direction, basis = "unknown", "flow_pending"
    raw_row = {"flow_rank_cross": flow_info.get("rank_cross", 50)}
    flow_gate = gates.compute_gates(raw_row, history=None)["flow"]
    return Row(
        ticker=ticker,
        spot=float(flow_info.get("spot") or 0.0),
        direction=direction,
        direction_basis=basis,
        is_synthetic=True,
        is_light=True,
        gates={"flow": flow_gate, "oi": "gray", "structural": "gray", "cost": "gray"},
        flow=Flow(
            alerts=int(flow_info.get("alerts", 0)),
            premium_usd=float(flow_info.get("premium_usd", 0.0)),
            rank_cross=int(flow_info.get("rank_cross", 50)),
        ),
        flow_alerts_detail=flow_alerts_detail,
        ask_side_pct=float(flow_info.get("ask_side_pct", 0.0)),
    )


async def build_light_snapshot() -> Snapshot:
    """Flow-only grid: one flow_alerts call → hot-15 ranked light rows. No
    per-ticker heavy endpoints, no regime (the front door attaches that)."""
    loop = asyncio.get_running_loop()
    now = datetime.now(tz=timezone.utc)
    flow_alerts = await _in_ctx(loop, partial(storage.fetch_flow_alerts, 100))
    if isinstance(flow_alerts, storage.UWFailure):
        log.error("light build: flow-alerts failed: %s", flow_alerts.message)
        return _empty_snapshot(now)
    hot_15 = universe.top_15_unique_tickers(flow_alerts)
    flow_by_ticker = _aggregate_flow_per_ticker(flow_alerts, hot_15)
    rows = [
        _light_row(t, flow_by_ticker.get(t, {"alerts": 0, "premium_usd": 0.0,
                                             "rank_cross": 50, "spot": 0.0}))
        for t in hot_15
    ]
    return Snapshot(fetched_at=now, regime=_current_regime(), rows=rows)
```

Notes for the implementer:
- `Row`, `Flow`, `Snapshot` are already imported in `snapshot.py`.
- If `Row(gates={...})` expects a typed `Gates`/dict, mirror how `_build_dashboard_row` constructs `gates=g` (a plain dict there) — pass the same shape.
- `_current_regime()` already exists as the `_empty_snapshot` regime fallback; the front door (Task 6) overwrites `snap.regime` with the real/carried-forward regime, so a placeholder here is fine.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_light_snapshot.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/snapshot.py tests/test_light_snapshot.py
git commit -m "feat(snapshot): build_light_snapshot — flow-only grid, one flow call"
```

---

### Task 6: `get_or_build_snapshot()` — two-window cache-or-build front door

**Files:**
- Modify: `server/snapshot.py` — add `get_or_build_snapshot`, module-level `_RAM = {"latest": None}`, env-read TTLs; reuse `_build_market_regime`, `build_light_snapshot`, `storage.append_snapshot`, `storage.read_last_snapshot`
- Test: `tests/test_get_or_build_snapshot.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_get_or_build_snapshot.py
import datetime as dt
import pytest
from server import snapshot as snap_mod
from server.schema import Snapshot, Row, Regime


def _snap(age_s, regime_posture="Stand down", regime_age_s=0):
    now = dt.datetime.now(tz=dt.timezone.utc)
    return Snapshot(
        fetched_at=now - dt.timedelta(seconds=age_s),
        regime=Regime(posture=regime_posture,
                      as_of=(now - dt.timedelta(seconds=regime_age_s)).isoformat()),
        rows=[Row(ticker="AAA", is_light=True)],
    )


@pytest.mark.asyncio
async def test_serves_cached_when_flow_fresh(monkeypatch):
    snap_mod._RAM["latest"] = _snap(age_s=5)
    built = []
    monkeypatch.setattr(snap_mod, "build_light_snapshot", lambda: _fail_build(built))
    out = await snap_mod.get_or_build_snapshot()
    assert built == [], "fresh flow → no rebuild"
    assert out.rows[0].ticker == "AAA"


@pytest.mark.asyncio
async def test_rebuilds_grid_but_carries_regime_when_flow_stale_regime_fresh(monkeypatch):
    cached = _snap(age_s=120, regime_posture="Favorable", regime_age_s=30)
    snap_mod._RAM["latest"] = cached
    monkeypatch.setattr(snap_mod, "build_light_snapshot", _fake_build)
    regime_calls = []
    monkeypatch.setattr(snap_mod, "_build_market_regime",
                        lambda loop: _record_regime(regime_calls))
    monkeypatch.setattr(snap_mod.storage, "append_snapshot", lambda d: True)
    out = await snap_mod.get_or_build_snapshot()
    assert regime_calls == [], "fresh regime carried forward, not recomputed"
    assert out.regime.posture == "Favorable"
    assert out.rows[0].ticker == "FRESH"


async def _fake_build():
    return Snapshot(fetched_at=dt.datetime.now(tz=dt.timezone.utc),
                    regime=Regime(), rows=[Row(ticker="FRESH", is_light=True)])

async def _record_regime(calls):
    calls.append(1)
    return (Regime(posture="NEW"), False)

def _fail_build(built):
    built.append(1)
    raise AssertionError("should not build")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_get_or_build_snapshot.py -v`
Expected: FAIL — `get_or_build_snapshot` / `_RAM` don't exist.

- [ ] **Step 3: Implement the front door**

Add to `server/snapshot.py`:

```python
import os

_RAM: dict[str, Snapshot | None] = {"latest": None}
_SNAPSHOT_MAX_AGE_S = int(os.environ.get("SNAPSHOT_MAX_AGE_S", "60"))
_REGIME_MAX_AGE_S = int(os.environ.get("REGIME_MAX_AGE_S", "600"))


def _age_s(iso_or_dt, now) -> float:
    if iso_or_dt is None:
        return 1e9
    if isinstance(iso_or_dt, str):
        try:
            t = datetime.fromisoformat(iso_or_dt.replace("Z", "+00:00"))
        except ValueError:
            return 1e9
    else:
        t = iso_or_dt
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds()


async def get_or_build_snapshot(*, force_flow: bool = False) -> Snapshot:
    """Cache-or-build front door. Per-namespace freshness: rebuild the flow grid
    when older than SNAPSHOT_MAX_AGE_S (or force_flow); recompute the regime only
    when older than REGIME_MAX_AGE_S, else carry the cached regime forward."""
    now = datetime.now(tz=timezone.utc)
    cached = _RAM.get("latest")
    if cached is None:
        cached_disk = storage.read_last_snapshot()
        if cached_disk:
            try:
                cached = Snapshot.model_validate(cached_disk)
                _RAM["latest"] = cached
            except Exception:
                cached = None

    flow_fresh = (not force_flow) and cached is not None and cached.rows and \
        _age_s(cached.fetched_at, now) < _SNAPSHOT_MAX_AGE_S
    if flow_fresh:
        return cached

    fresh = await build_light_snapshot()
    if not fresh.rows and cached is not None and cached.rows:
        return cached   # never overwrite good data with an empty build

    # Regime: carry forward when still fresh, else recompute.
    regime_fresh = cached is not None and getattr(cached.regime, "posture", "") and \
        _age_s(getattr(cached.regime, "as_of", None), now) < _REGIME_MAX_AGE_S
    if regime_fresh:
        fresh.regime = cached.regime
    else:
        loop = asyncio.get_running_loop()
        regime_obj, _event = await _build_market_regime(loop)
        if regime_obj is not None:
            fresh.regime = regime_obj

    _RAM["latest"] = fresh
    storage.append_snapshot(fresh.model_dump(mode="json"))
    return fresh
```

Notes:
- Ensure `Regime` has an `as_of` field used for aging. If it doesn't, add `as_of: str | None = None` to `Regime` in `server/schema.py` and have `_build_market_regime` stamp it (`regime.as_of = now.isoformat()`), so the regime's own age is trackable. (Add this as part of this task — it's required for the carry-forward test.)
- `_build_market_regime` currently returns `(Regime, event_within_hold)`; we use the Regime and ignore the flag here (the per-ticker cost gate gets the flag during a full row build on click).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_get_or_build_snapshot.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/snapshot.py server/schema.py tests/test_get_or_build_snapshot.py
git commit -m "feat(snapshot): get_or_build_snapshot two-window front door (flow vs regime TTL)"
```

---

### Task 7: Routes call the front door + a refresh route

**Files:**
- Modify: `server/main.py` — `root` (`203-212`), `snapshot_json` (`330-335`), add refresh route; use `snapshot_mod.get_or_build_snapshot` and keep `_snapshot_cache`/`_RAM` consistent
- Test: `tests/test_routes_basic_platform.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routes_basic_platform.py
from fastapi.testclient import TestClient


def test_snapshot_json_builds_on_request(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REPLAY", "1")  # no live UW; serve archive/empty honestly
    from server.main import app
    with TestClient(app) as c:
        r = c.get("/snapshot.json")
        assert r.status_code == 200
        body = r.json()
        assert "rows" in body


def test_refresh_route_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REPLAY", "1")
    from server.main import app
    with TestClient(app) as c:
        r = c.get("/snapshot.json?refresh=1")
        assert r.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_routes_basic_platform.py -v`
Expected: FAIL or error until routes call the new front door and `_refresh_loop` removal (Task 8) lands; if it passes incidentally because the loop seeded a cache, the `?refresh=1` branch will still be unverified. Proceed.

- [ ] **Step 3: Wire the routes to the front door**

In `server/main.py`, make `/snapshot.json` build on request and support `refresh`:

```python
@app.get("/snapshot.json")
async def snapshot_json(refresh: int = 0):
    snap = await snapshot_mod.get_or_build_snapshot(force_flow=bool(refresh))
    _snapshot_cache["latest"] = snap   # keep _resolve_row (tile3/tile4) in sync
    if snap is None or not snap.rows:
        return JSONResponse({"status": "warming", "rows": []}, status_code=200)
    return JSONResponse(snap.model_dump(mode="json"))
```

Make `/` build on request too:

```python
@app.get("/", response_class=HTMLResponse)
async def root():
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    snap = await snapshot_mod.get_or_build_snapshot()
    _snapshot_cache["latest"] = snap
    snapshot_json = "null" if (snap is None or not snap.rows) else \
        json.dumps(snap.model_dump(mode="json"), default=str)
    hydration = f"<script>window.__SNAPSHOT__ = {snapshot_json};</script>"
    return HTMLResponse(html.replace(_HYDRATION_MARKER, hydration))
```

Notes:
- `_resolve_row` (used by `/api/tile3` and `/api/tile4`) reads `_snapshot_cache["latest"]`; keeping it assigned here means a click on a light grid row resolves the (light) row, and `build_single_row`/lookup upgrades it. The existing click-resilience (`reason:"not in snapshot"`) covers rotated-out tickers.
- `_RAM` (in snapshot.py) is the source of truth for the front door; `_snapshot_cache` is the tile-route resolver mirror. Both point at the same `Snapshot` object — acceptable.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_routes_basic_platform.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/main.py tests/test_routes_basic_platform.py
git commit -m "feat(api): / and /snapshot.json build on request; ?refresh=1 forces flow"
```

---

### Task 8: Remove the background loop + loop-only knobs

**Files:**
- Modify: `server/main.py` — delete `_refresh_loop` (`160-194`), `_next_cached_snapshot` (`135-157`), `_REFRESH_INTERVAL_SECONDS`/`_CLOSED_RECHECK_SECONDS` (`55-58`), and the loop task in `lifespan` (`114-132`)
- Modify: `server/snapshot.py` — remove `refresh_snapshot` (now unused) IF nothing else references it (grep first); keep `build_single_row`, `_build_dashboard_row`, `_build_market_regime`
- Test: `tests/test_no_background_loop.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_no_background_loop.py
import asyncio
from fastapi.testclient import TestClient


def test_no_background_task_started(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REPLAY", "1")
    from server.main import app
    before = len(asyncio.all_tasks(asyncio.new_event_loop())) if False else None  # noqa
    with TestClient(app) as c:
        c.get("/health")
    # The app must define no _refresh_loop symbol anymore.
    import server.main as m
    assert not hasattr(m, "_refresh_loop"), "background loop must be removed"
    assert not hasattr(m, "_REFRESH_INTERVAL_SECONDS")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_no_background_loop.py -v`
Expected: FAIL — `_refresh_loop` still defined.

- [ ] **Step 3: Delete the loop and its scaffolding**

In `server/main.py`:
- Delete `_REFRESH_INTERVAL_SECONDS` and `_CLOSED_RECHECK_SECONDS`.
- Delete `_next_cached_snapshot` and `_refresh_loop` entirely.
- Simplify `lifespan` to:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    budget.load_persisted()
    _seed_cache_from_disk()          # tail-seed last build into _snapshot_cache
    # Also prime snapshot._RAM so the first request can serve last-good instantly.
    if _snapshot_cache.get("latest") is not None:
        snapshot_mod._RAM["latest"] = _snapshot_cache["latest"]
    yield
```

- Remove the now-unused `asyncio` import only if nothing else uses it (the routes use `asyncio.to_thread` — keep it).
- Update the module docstring (`1-7`) to drop "refreshes snapshot every 60s".

In `server/snapshot.py`: grep for `refresh_snapshot`. If only tests reference it, delete `refresh_snapshot` and update/remove those tests (e.g. `tests/test_snapshot.py` cases that called it) to use `get_or_build_snapshot`/`build_light_snapshot` instead. Keep `_build_dashboard_row`, `build_single_row`, `_build_market_regime`, `_empty_snapshot`, `_current_regime`.

- [ ] **Step 4: Run test + full suite**

Run: `.venv/Scripts/python -m pytest tests/test_no_background_loop.py -v`
Expected: PASS.
Run: `.venv/Scripts/python -m pytest -q`
Expected: PASS (fix or retire any `refresh_snapshot`-based tests surfaced here).

- [ ] **Step 5: Commit**

```bash
git add server/main.py server/snapshot.py tests/
git commit -m "refactor: remove background refresh loop; request-driven only"
```

---

### Task 9: Frontend — light grid render + provisional direction + full build on click

**Files:**
- Modify: `static/index.html` — `renderWatchlist` (`1355-1408`), `selectTicker` (`1415-1423`)
- Test: `tests/test_html_preservation.py` (verify still green; update only if a guarded anchor moves)

- [ ] **Step 1: Render light rows in `renderWatchlist`**

In the `for (const row of sorted)` loop, replace the `dirArrow` and `dots` construction to honor `is_light`:

```javascript
    const isLight = row.is_light;
    const dirArrow = row.direction === "calls" ? "↑ Calls"
                   : row.direction === "puts" ? "↓ Puts"
                   : "· dir on click";
    const dirProvisional = isLight && (row.direction === "calls" || row.direction === "puts");
    const dirHtml = `<span class="direction ${row.direction}${dirProvisional ? " provisional" : ""}"`
                  + `${dirProvisional ? ' title="flow-only — gamma confirmed on click"' : ""}>`
                  + `${dirArrow}${dirProvisional ? " <em>(flow-only)</em>" : ""}</span>`;
    const dots = ["flow", "oi", "structural", "cost"].map(g => {
      const c = isLight && g !== "flow" ? "gray" : row.gates[g];
      const ttl = isLight && g !== "flow" ? `Gate ${g}: click to evaluate` : `Gate ${g}: ${row.gates[g]}`;
      return `<span class="dot ${c}" title="${ttl}"></span>`;
    }).join("");
```

Then use `dirHtml` in place of the old `<span class="direction ...">${dirArrow}</span>` in `tr.innerHTML`. Add CSS near the existing `.direction` rules:

```css
  .row .direction.provisional { opacity: 0.6; font-style: italic; }
  .row .direction.provisional em { font-size: 0.8em; opacity: 0.8; }
```

- [ ] **Step 2: Trigger full build on click for light rows in `selectTicker`**

Replace `selectTicker`:

```javascript
function selectTicker(ticker) {
  state.selected = ticker;
  state.tile3Expiry = null;
  state.tile3Weighting = "oi";
  const row = ROWS.find(r => r.ticker === ticker);
  if (row && row.is_light && state.lookups[ticker] !== "loading" && state.lookups[ticker] !== "ok") {
    // Flow-only grid row → build the full row on demand, then it renders like a hot one.
    lookupTicker(ticker);           // replaces the light row + re-selects
    renderWatchlist();
    renderDeepDive();
    return;
  }
  fetchTile3Detail(ticker);
  fetchTile4Detail(ticker);
  renderWatchlist();
  renderDeepDive();
}
```

(Confirm `lookupTicker`, on success, replaces the row in `ROWS` — it does `ROWS.push` only if absent; since the light row already exists, update it: in `lookupTicker`'s success branch, replace an existing row of the same ticker with the full `d`. Add, in `lookupTicker` where it currently does `if (!ROWS.find(...)) ROWS.push(d)`:)

```javascript
        const idx = ROWS.findIndex(r => r.ticker === d.ticker);
        if (idx >= 0) ROWS[idx] = d; else ROWS.push(d);
```

- [ ] **Step 3: Verify offline via replay + Playwright**

Run a replay server and confirm: light rows show flow dot colored, other three gray; provisional direction badge present; clicking a light row triggers a lookup and the deep-dive renders. (No JS unit harness in this repo; this is the project's standard frontend verification — see CLAUDE.md.)

```bash
DATA_DIR=./data REPLAY=1 .venv/Scripts/python -m uvicorn server.main:app --port 8013
```

- [ ] **Step 4: Keep the preservation test green**

Run: `.venv/Scripts/python -m pytest tests/test_html_preservation.py -q`
Expected: PASS (6 zones intact; render functions present). If a guarded anchor legitimately moved, update the test to match intent (per CLAUDE.md) — don't weaken it.

- [ ] **Step 5: Commit**

```bash
git add static/index.html tests/test_html_preservation.py
git commit -m "feat(ui): light grid rows — provisional direction + full build on click"
```

---

### Task 10: Frontend — loud staleness, refresh-grid button, deep-dive own as_of

**Files:**
- Modify: `static/index.html` — `renderRegimeBanner` (`1255-1294`) for loud staleness; add a refresh button + handler; deep-dive header `as_of`
- Test: `tests/test_html_preservation.py` (keep green)

- [ ] **Step 1: Loud staleness + refresh button**

In `renderRegimeBanner`, past the flow window, make staleness prominent and add an explicit "as of" + refresh affordance. Replace the age/detail tail:

```javascript
  const fetchedAt = new Date(snap.fetched_at);
  const ageSec = Math.floor((Date.now() - fetchedAt.getTime()) / 1000);
  ageEl.textContent = ageSec < 60 ? `${ageSec}s ago`
                    : ageSec < 3600 ? `${Math.floor(ageSec / 60)}m ago`
                    : `${Math.floor(ageSec / 3600)}h ago`;
  const STALE_S = 60;
  const hhmm = fetchedAt.toISOString().slice(11, 16);
  const staleHtml = ageSec > STALE_S
    ? ` · <span class="flow-stale">flow as of ${hhmm}Z — ${ageEl.textContent} · </span>`
    : " · ";
  detailEl.innerHTML = `${snap.rows.length} tickers${staleHtml}`
    + `<button id="refresh-grid-btn" class="refresh-grid">refresh flow</button>`;
  if (ageSec > 300) ageBanner.classList.add('stale'); else ageBanner.classList.remove('stale');
  const btn = document.getElementById("refresh-grid-btn");
  if (btn) btn.onclick = refreshGrid;
```

Add CSS:

```css
  .flow-stale { color: var(--warn); font-weight: 700; }
  .refresh-grid { font: inherit; color: var(--accent); background: none; border: 1px solid var(--axis);
                  border-radius: 4px; padding: 1px 8px; cursor: pointer; }
```

- [ ] **Step 2: Add the one-call refresh handler (grid + header only)**

Add near `selectTicker`:

```javascript
/* Manual, user-initiated flow refresh (1 call). Re-pulls /snapshot.json?refresh=1
 * and re-renders ONLY the grid + header — never the open deep-dive (operator rule:
 * don't refresh under the cursor). */
async function refreshGrid() {
  try {
    const r = await fetch('/snapshot.json?refresh=1');
    if (!r.ok) return;
    const snap = await r.json();
    if (!snap.rows) return;
    const preserved = ROWS.filter(row =>
      !snap.rows.find(x => x.ticker === row.ticker) &&
      (row.__lookedup || row.ticker === state.selected));
    ROWS.length = 0;
    ROWS.push(...snap.rows, ...preserved);
    window.__SNAPSHOT__ = snap;
    renderRegimeBanner();
    renderWatchlist();
    // intentionally NO renderDeepDive()
  } catch (e) { console.warn('refresh failed:', e); }
}
```

- [ ] **Step 3: Deep-dive carries its own as_of**

In `renderDeepDive` (the deep-dive header area), if the selected row carries an `as_of` (full rows from `/api/lookup` include `as_of`), render it so a refreshed grid row and an older open deep-dive read as two labeled ages. Add to the deep-dive title block:

```javascript
  const ddAsOf = (row && row.as_of) ? `<span class="dd-asof">deep-dive as of ${new Date(row.as_of).toISOString().slice(11,16)}Z</span>` : "";
```

and include `${ddAsOf}` in the title HTML. Add CSS `.dd-asof { color: var(--text-dim); font-size: 0.8em; margin-left: 8px; }`.

- [ ] **Step 4: Verify offline via replay + Playwright**

With the replay server, confirm: a >60s-old snapshot shows the loud "flow as of …" + "refresh flow" button; clicking refresh re-pulls and re-renders the grid without disturbing an open deep-dive; the deep-dive shows its own as_of.

- [ ] **Step 5: Keep preservation test green + commit**

Run: `.venv/Scripts/python -m pytest tests/test_html_preservation.py -q` → PASS.

```bash
git add static/index.html tests/test_html_preservation.py
git commit -m "feat(ui): loud staleness + one-call refresh-flow button + deep-dive as_of"
```

---

### Task 11: Verify, smoke, finish

**Files:** none (verification) — then the finishing skill.

- [ ] **Step 1: Full suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all pass, live tests skipped.

- [ ] **Step 2: Call-count bound check (no silent fan-out)**

Confirm the light-build test (Task 5) still asserts zero per-ticker heavy fetches, and add/keep an assertion that `get_or_build_snapshot` with a fresh cache makes zero UW calls. (These are the regression guards against accidentally fanning back out to all-15.)

- [ ] **Step 3: REPLAY smoke**

```bash
DATA_DIR=./data REPLAY=1 .venv/Scripts/python -m uvicorn server.main:app --port 8013
```
Confirm: page loads a light grid instantly; clicking a ticker builds the full deep-dive (tile3-rich/tile4 from archive); refresh button works; no background task; staleness loud.

- [ ] **Step 4: Update docs**

Update `README.md` and `CLAUDE.md` architecture notes: the 120s loop is gone; describe the request-driven model, the two append-only logs, per-namespace TTLs, and the refresh button. Note the CLAUDE.md refinement (percentile foundation = future thin daily writer, not the view-driven archive).

- [ ] **Step 5: Finish the branch**

Use **superpowers:finishing-a-development-branch** (verify tests → present the 4 options → execute). Expected operator choice: merge to main + push (Railway auto-deploys).

---

## Deferred (flagged, not built — YAGNI now)

- **Part-file retention/prune** — a "keep last N part files per partition" sweep. Deferred, but it's a *read-path* cost (glob+sort grows per read for a repeatedly-viewed ticker), not just disk. Revisit when read latency on a hot ticker is noticeable.
- **Thin daily writer** — required *before* the v0.2 percentile gates are un-stubbed (view-driven archive is attention-biased). Separate plan when percentiles become real.
- **Snapshot-log daily rotation** — the line-cap (Task 3) bounds growth; dated-file rotation is a nicety to add only if audit-by-day matters.

## Self-Review

**Spec coverage:** remove loop (T8) · read-through cache + append-only logs (T1 raw archive, T3 snapshot log) · per-namespace TTL (T6) · light grid + provisional direction (T5, T9) · full build on click (T9) · loud staleness + refresh button (T10) · atomic per-write parquet (T1) · Tile2 multi-session aggregation across fragmented files (T2) · snapshot-log tail-seed + cap (T3) · deep-dive own as_of (T10) · REPLAY preserved (T7/T11) · history-bias/percentile ordering (Deferred section) · error handling: empty-build never overwrites good (T6), flow failure → last-good (T6/T5). All spec sections map to a task.

**Placeholder scan:** every code step has complete code; verification steps name exact commands. Frontend tasks use replay+Playwright (the repo's actual frontend verification per CLAUDE.md) rather than a non-existent JS unit harness — stated honestly, not a placeholder.

**Type consistency:** `is_light` (T4) used in T5/T9; `get_or_build_snapshot(*, force_flow=False)` (T6) called in T7 (`force_flow=bool(refresh)`); `build_light_snapshot()` (T5) called in T6; `Regime.as_of` (T6 note) used by the carry-forward logic and T10 deep-dive; `write_response` (real name) used in T1/T2 tests; `_RAM` (T6) primed in T8 lifespan and read in T6.
