# Ops / CI — Design (v3, Phase 6)

**Status:** PLAN (awaiting operator approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** all phases (threaded throughout; finalized here)
**Starting point:** existing `Dockerfile`, `railway.toml` — do not change either.

## Purpose
Make correctness visible at every merge and every deploy: path lint, golden tests,
REPLAY parity, and the no-client-computation check all gate the build; nightly backup
and compaction run outside the request path. Nothing here is novel infrastructure — it
is discipline applied to the v3 pipeline via the tools already in the stack.

## Approach

### 1. Hyphenated-path lint at CI

UW underscore-for-hyphen is a silent 404, the hardest class of v2 bugs. Two layers:

**Runtime guard (already in `uw_client.assert_hyphenated`):** raises `ValueError` at
call time if any path segment contains an underscore. This fires in both test and prod.

**CI lint test (`tests/test_uw_paths.py`, port from `e1d6c5e`):**
```python
# grep all string literals in server/ that look like UW endpoint paths
# assert none contain underscores in resource segments (e.g. "flow_alerts" → fail)
```
Keeper: `git show e1d6c5e:tests/test_uw_paths.py` — the regex + path collection
logic; update imports to point to v3's `uw_client`. Runs on every push; zero dependencies
on network or UW key.

### 2. Golden tests gate deploy

`tests/fixtures/bronze/` contains real captured payloads (Phase 2). The normalize and
derive test suites run against them offline. These are the load-bearing tests.

**Gate rule:** any test in `tests/` that exercises a golden fixture is part of the
default test run (`pytest` with no extra flags). A failing golden test blocks the deploy.
No `@pytest.mark.live` or skip markers on golden tests — they are always offline-safe.

**The probe (`scripts/probe_endpoints.py`) is not a CI test** — it requires a live UW
key and runs on demand (`railway run …`) before UW-touching changes, not on every push.

### 3. No-client-computation CI check

The one rule: the frontend computes nothing. A CI step greps `static/` for signal or
decision logic and fails the build if any is found.

**Implementation (`tests/test_no_client_compute.py`):**
```python
import re, pathlib
BANNED = [
    r"\bvolume_oi_ratio\b",      # direction proxy — server only
    r"\bgex_sign\b",             # gamma sign — server only
    r"\bIVR\b|\biv_rank\b",      # cost gate — server only
    r"\bnew Date\(\)",           # clock-for-decision in client — server only
    r"\bif.*direction.*===",     # decision branch on direction field
]
SRC = (pathlib.Path("static") / "index.html").read_text()
for pat in BANNED:
    assert not re.search(pat, SRC), f"client-side computation detected: {pat!r}"
```
The banned list starts from the v2 CALIBRATION block patterns (identified in
`docs/architecture.md` as the root of the split-brain bug class). Extend as new signals
land in Phase 4. Test is part of the default `pytest` run; no live dependency.

### 4. REPLAY=1 produces an identical view model offline in CI

`REPLAY=1` forces the governor to deny all live calls; ingest reads bronze directly.
CI verifies parity: the view model produced from captured bronze offline must match
the recorded golden view model.

**Implementation (`tests/test_replay_parity.py`):**
```python
# Setup: set REPLAY=1, DATA_DIR pointing to tests/fixtures/
# Call: GET /api/view/SPY → ViewModel (JSON)
# Assert: deep-equal to tests/fixtures/golden_viewmodel/SPY.json
# The golden view model is captured once per bronze refresh (scripts/capture_golden_vm.py)
# and committed; CI compares against it.
```
If the view model changes (e.g. a new field), the golden snapshot is re-captured via
the capture script and committed. CI is not the place to discover structural drift —
that is a Phase 2/3 action; CI is the place to confirm nothing drifted unintentionally.

**REPLAY=1 in CI:** set via the `pytest` env fixture (`@pytest.fixture(autouse=...)` or
`pyproject.toml` env override); no UW key needed in CI.

### 5. Nightly bronze backup to object storage

Bronze is the irreplaceable raw log. It must survive a Railway ephemeral-volume loss.

**Schedule:** Railway cron `0 2 * * *` (2 AM ET, outside market hours).
**Script:** `scripts/backup_bronze.py` — tarballs `DATA_DIR/bronze/` and streams to
an object storage bucket (operator-chosen: Railway volume snapshot, an S3-compatible
store, or litterbox for manual download). Uses `BACKFILL_TOKEN` for auth if the
backup hits the `/admin/export` endpoint.

**Not in the request path:** the backup script never imports anything from the FastAPI
app's request-handling layer. It is a standalone script invoked by cron.

**Keeper:** `git show e1d6c5e:scripts/pull_archive.py` — the tar extraction + DATA_DIR
layout; `backup_bronze.py` is the inverse (tar + upload). The existing `/admin/export`
endpoint (token-guarded by `BACKFILL_TOKEN`) streams the tar.gz and can be used as the
backup source if object storage credentials are not set up.

### 6. Compaction cron (separate from runtime, never request-path)

Append-only writes accumulate many small part files. Compaction merges them per
partition into one file, reducing DuckDB scan overhead.

**Schedule:** Railway cron `0 3 * * *` (3 AM ET, after backup).
**Script:** `scripts/compact.py`:
1. For each `(tier, endpoint, dt, ticker)` partition with > N part files:
   a. DuckDB `SELECT * FROM read_parquet(glob)` → one merged Arrow table.
   b. Write via `storage.write_part` (atomic temp→os.replace, new part name).
   c. Delete the input files only after the merged file is confirmed on disk
      (`os.path.exists(merged)` check before any unlink).
2. Compaction never touches bronze (immutable by policy). Only silver + gold are
   compacted once their derive/decide writes accumulate.
3. Compaction never runs in the request path; the FastAPI app never calls it.
   The runtime only appends and queries.

**Idempotent:** a crashed compaction leaves orphan part files; the next run re-merges
them. The DuckDB read layer handles multiple part files per partition correctly.

### 7. Deploy unchanged — Dockerfile + railway.toml

The existing deploy artifacts are correct and deployed green. This spec does not change them.

- **`Dockerfile`:** Python 3.11, pip install, `CMD ["uvicorn", "server.main:app", ...]`.
- **`railway.toml`:** healthcheck `/health`; env vars injected by Railway.
- **Healthcheck (`GET /health`):** returns `{"status": "ok", "replay": bool}`. Already
  in the v3 scaffold; no change.

CI tests run in a `python:3.11` GitHub Actions runner (or Railway's build step) with
`pip install -r requirements.txt && pytest`. No Docker needed for CI.

---

## Acceptance criteria

- [ ] `tests/test_uw_paths.py` (path lint) passes on every push with no UW key.
- [ ] `tests/test_no_client_compute.py` fails if `static/index.html` contains any of
      the BANNED patterns, passes on a clean frontend.
- [ ] `tests/test_replay_parity.py` passes offline (`REPLAY=1`, no UW key): the
      ViewModel from captured bronze matches the committed golden snapshot.
- [ ] All golden tests (normalize + derive, running against `tests/fixtures/bronze/`)
      pass without network access; failure blocks deploy.
- [ ] `scripts/backup_bronze.py` runs to completion in CI dry-run (tar + checksum,
      no upload) and exits 0.
- [ ] `scripts/compact.py` round-trips: appends two parts, compacts, DuckDB reads the
      merged result; original parts deleted only after merge confirmed.
- [ ] Railway cron entries for backup (2 AM) and compaction (3 AM) are documented in
      `railway.toml` or a `crons.md` operations note; cron scripts never import FastAPI
      request-handling code.
- [ ] `Dockerfile` and `railway.toml` are unchanged (a diff against the current file
      shows no modification).

## Definition of done (universal — from the plan)

Typed in/out; provenance on every value; no boundary skipped; REPLAY-reproducible;
golden tests assert a sane non-None value (not just field presence).

## Defers to operator

- Object storage destination for nightly backup (Railway volume snapshot / S3-compatible
  / manual litterbox pull — operator chooses; `backup_bronze.py` accepts a `BACKUP_DST`
  env var and falls back to a local tar if unset).
- Compaction threshold N (number of part files before merging); recommend N=10 as a
  starting value, tunable via env var.
- Whether the GitHub Actions workflow is added now (Phase 6) or the Railway build step
  is used instead — both run the same `pytest` command; the operator decides the runner.

## Open questions / flags

- The no-client-compute BANNED list must be maintained as signals land in Phase 4.
  Each new signal in `derive` gets a corresponding banned pattern added to the test.
  Recommend: a comment block in `test_no_client_compute.py` listing which Phase each
  pattern was added for, so the reviewer can verify coverage.
- `tests/test_replay_parity.py` requires a committed `tests/fixtures/golden_viewmodel/SPY.json`.
  That file is generated after Phase 3's walking skeleton is deployed. Phase 6 finalizes
  the test; stub the file path in Phase 3.
- If Railway cron is not available on the current plan tier, backup and compaction are
  documented as manual `railway run …` commands and the operator runs them on a schedule.
  Confirm tier support before scripting the Railway cron config.
