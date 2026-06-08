# Golden Bronze + Data Unknowns — Design (v3, Phase 2)

**Status:** PLAN (awaiting operator approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** Phase 1 (uw_client + storage scaffold)
**BLOCKS:** Phase 3 (walking skeleton) — no signal math is trusted until data unknowns are signed off.

## Purpose
Capture real UW payloads verbatim into `tests/fixtures/bronze/` and resolve the five
data unknowns empirically, with the probe command and observed result committed for each.
Bronze fixtures become the permanent ground truth that normalize and derive tests run
against; assumptions about field shapes, signs, and truncation are killed here, not downstream.

## Approach

### 1. Capture golden bronze via the Railway bridge

The prod Railway service has `UW_API_KEY` injected. Pull real payloads through it rather
than requiring a local key, so offline CI can run against the same captures.

```powershell
# Set the token once per shell
$env:RAILWAY_API_TOKEN = [Environment]::GetEnvironmentVariable("RAILWAY_API_TOKEN","User")

# For each endpoint, pull verbatim and write to tests/fixtures/bronze/
railway run python scripts/capture_golden.py --ticker SPY --endpoints \
  flow-alerts greek-flow net-prem-ticks oi-per-strike historical-risk-reversal-skew

# Or run the probe, which pulls and reports in one pass:
railway run python scripts/probe_endpoints.py SPY
```

`scripts/capture_golden.py` (new, thin wrapper over `uw_client.get`): hits each endpoint
once with the canonical params the pipeline will use, writes the raw JSON verbatim to
`tests/fixtures/bronze/<endpoint>/<ticker>.json` with a `_meta` sidecar
`{fetched_at, params, status, content_hash}`. Writes are atomic (temp→os.replace).
Files are committed to the repo; they are the offline source for all normalize + derive tests.

Keeper to port for the Railway probe pattern:
`git show e1d6c5e:scripts/probe_endpoints.py` — PROBES list, `_sane_*` invariant fns,
per-endpoint key-presence + value-sanity reporting, exit≠0 on error/warn. Re-home into
`scripts/probe_endpoints.py` with paths updated to v3's `uw_client.get`.

### 2. Probe script re-homed (v3)

`scripts/probe_endpoints.py` (port from `e1d6c5e:scripts/probe_endpoints.py`):
- Calls `server.services.uw_client.get` directly (bypasses storage/cache — tests real paths).
- Reports per endpoint: HTTP outcome, row count, required-key presence, value-sanity invariants.
- Exit code non-zero on any ERROR or SANITY fail; suitable as a CI gate pre-deploy
  (operator runs manually on live, CI runs against captured bronze).

### 3. Resolve each data unknown — probe + assert + record

For each unknown: run the probe, record the command and observed result in this spec
(filled in at Phase 2 execution time), commit the finding. Operator signs off before Phase 3.

---

## Data unknown (a) — greek-flow sign convention

**What is unknown:** whether `dir_delta_flow` in the UW `greek-flow` response carries a
sign where negative = net put-side / ask-side print and positive = net call-side, or the
reverse. v2 shipped a sign caveat in code because this was never empirically validated on
a clean session.

**Probe command:**
```powershell
$env:RAILWAY_API_TOKEN = [Environment]::GetEnvironmentVariable("RAILWAY_API_TOKEN","User")
railway run python scripts/probe_greek_flow_sign.py --ticker SPY --date 2026-06-05 --minute "15:32"
```

`scripts/probe_greek_flow_sign.py` (new, thin):
1. Fetches `greek-flow` for SPY for 2026-06-05 (or the nearest clean session available).
2. Isolates the 3:32 PM ET minute (known ask-side put print from market context).
3. Asserts `dir_delta_flow[15:32] < 0` (ask-side put = negative directional delta).
4. Cross-checks: `sum(dir_delta_flow)` over the session vs `total_delta_flow` (which is
   tape-consistent and a known anchor); divergence is flagged, never used to calibrate sign.
5. Also checks `net_delta` from `net-prem-ticks` for the same minute — the two must agree
   in sign on the 3:32 PM row.

**What to assert:**
- The 3:32 PM ET minute row: `dir_delta_flow < 0`.
- Over a clean unidirectional session (all prints ask-side puts): `sum(dir_delta_flow) < 0`.
- `dir_delta_flow` and `net_delta` (from net-prem-ticks) agree in sign for that minute.

**Calibration rule (NEVER violate):** sign must be pinned on a session with a clean
known-direction event. Do NOT calibrate on a session where calls and puts cross — the
sign is invisible on a mixed day. Use the 6/5 3:32 PM ask-side put as the anchor.

**Where the answer is recorded:** Fill in the `_FINDING` block below at execution time.

```
_FINDING (a): greek-flow sign
  probe_command: <paste exact command>
  session_date: <YYYY-MM-DD>
  dir_delta_flow at 15:32: <observed value>
  sign_correct: yes | no | inconclusive
  net_prem_ticks cross_check: <agrees | disagrees | field missing>
  resolution: <"vendor sign: negative = put/ask-side; field confirmed">
```

---

## Data unknown (b) — net-prem-ticks population

**What is unknown:** whether `net_call_premium`, `net_put_premium`, and `net_delta` in
the `net-prem-ticks` response actually populate intraday (v2 once saw `net_premium = 0`
across the whole series), and whether the per-minute cumulative series is non-degenerate
(not all zeros, not all the same value).

**Probe command:**
```powershell
railway run python scripts/probe_endpoints.py SPY
```
The probe's `_sane_*` invariants cover field presence; add a net-prem-ticks-specific
invariant: assert `max(|net_delta|) > 0` across the series (not all zeros).

**What to assert:**
- `net_call_premium`, `net_put_premium`, `net_delta` are all present (non-null) on at
  least one row.
- `max(abs(row["net_delta"]) for row in rows) > 0` (series is not uniformly zero).
- The series has at least N rows (where N is the session minute count — flag if < 60).

**Operator decision required:** if `net_delta` == `dir_delta_flow` from greek-flow (same
field, two endpoints), decide whether to ingest `net-prem-ticks` at all or use it only as
the greek-flow sign cross-check (plan §Operator flags #2). Record the decision here.

```
_FINDING (b): net-prem-ticks population
  probe_date: <YYYY-MM-DD>
  net_call_premium populated: yes | no
  net_put_premium populated: yes | no
  net_delta max(|x|): <value>
  series_length_rows: <count>
  is_net_delta_same_as_dir_delta_flow: yes | no | not_checked
  operator_decision: <ingest separately | cross-check only | drop>
```

---

## Data unknown (c) — flow-alerts truncation

**What is unknown:** the exact record or time limit on `flow-alerts`, and whether
a pull returns the full session or only the last N alerts. v2 observed 118 records
on one pull and 394 on the next (cap appeared to be 500; older_than pagination exists).

**Probe command:**
```powershell
railway run python scripts/probe_flow_truncation.py --ticker SPY --limit 500
```

`scripts/probe_flow_truncation.py` (new):
1. Fetches flow-alerts with `limit=500`.
2. Records the oldest `created_at` timestamp and the newest `created_at` timestamp.
3. Computes the window span (newest − oldest) in hours.
4. Paginates via `older_than` with the oldest timestamp to check if more rows exist
   (one additional fetch with `older_than=<oldest_ts>`; if rows returned, cap < session).
5. Reports: total rows, window span, whether the session is complete (no older rows) or
   truncated (older rows exist), and the apparent page cap.

**What to assert:**
- Record the cap (500 or otherwise) from observed row count at limit=500.
- Whether `older_than` pagination yields additional rows (truncation confirmed or ruled out).
- The window span: is it the full market session (6.5 hours) or only the tail?

```
_FINDING (c): flow-alerts truncation
  probe_date: <YYYY-MM-DD>
  rows_at_limit_500: <count>
  oldest_created_at: <timestamp>
  newest_created_at: <timestamp>
  window_hours: <float>
  older_than_yields_more: yes | no
  resolution: <"session is last N alerts, cap=N, paginate via older_than" | "full session">
```

---

## Data unknown (d) — OI live-vs-settled and lookback depth

**What is unknown:** what `oi-per-strike` returns intraday (forming vs settled), what
`date=` returns vs no date, the settlement publish time, and the actual lookback depth
(v2's backfill.py docstring claims 30 days but that is a comment, not code-proven).

**Keeper for the date= pattern:** `git show e1d6c5e:server/backfill.py` — `_fetch_oi`
passes `date=d.isoformat()`, `_probe_ok` probes the oldest and newest days in the window
to find the actual depth. Port this probe logic.

**Probe command:**
```powershell
railway run python scripts/probe_oi_depth.py --ticker SPY
```

`scripts/probe_oi_depth.py` (new, ported from `e1d6c5e:server/backfill.py::_probe_ok`):
1. Fetches `oi-per-strike` with no `date=` (intraday). Records row count + a sample
   `call_oi` value + whether `settled` flag or similar field is present.
2. Fetches `oi-per-strike` with `date=<yesterday>`. Records row count + sample.
3. Binary-searches the lookback: fetches `date=<today - N days>` for N in
   [1, 7, 14, 21, 30, 45, 60] until it gets an empty response. Records the last
   successful N and the first empty N (= actual depth boundary).
4. Records the time of the probe (ET) to establish whether intraday == forming.

**What to assert:**
- Whether no-`date=` and `date=<yesterday>` return different row counts (if same, OI is
  settled-only intraday — a clock cadence implication).
- The exact lookback depth N (last successful date=), with the probe timestamp as context.
- Whether a `settled` field exists; if not, how the clock must gate the cadence.

```
_FINDING (d): OI live-vs-settled and lookback depth
  probe_date: <YYYY-MM-DD>  probe_time_et: <HH:MM>
  intraday_rows (no date=): <count>
  yesterday_rows (date=): <count>
  same_data_intraday_vs_settled: yes | no | unclear
  lookback_depth_days: <last successful N>
  first_empty_N: <N>
  settled_field_present: yes | no
  clock_cadence_implication: <"OI is forming intraday" | "OI is settled-only, date= required">
```

---

## Data unknown (e) — hyphenated paths return 200

**What is unknown:** confirmation that every endpoint path the v3 pipeline calls uses
hyphens (not underscores) and returns HTTP 200 in the current UW Basic tier.

**Probe command:**
```powershell
railway run python scripts/probe_endpoints.py SPY
```
The probe's ERROR rows catch any path returning 4xx. Any underscore in a path segment
is caught by `uw_client.assert_hyphenated()` before the call (raises ValueError at
call time — surfaces in the probe as an ERROR).

**What to assert:**
- Every endpoint in the PROBES list exits with `OK` or `EMPTY` (200 row); no `ERROR`.
- `assert_hyphenated()` passes for every path in `uw_client.py`.
- The CI path-lint test (`tests/test_uw_paths.py`, ported from `e1d6c5e`) confirms at
  build time.

```
_FINDING (e): hyphenated paths
  probe_date: <YYYY-MM-DD>
  errors: <list of endpoint+status or "none">
  underscore_violations: <list or "none">
  resolution: <"all 200, all hyphenated" | list of fixes applied>
```

---

## Keepers to port

- `git show e1d6c5e:scripts/probe_endpoints.py` — PROBES list, `_sane_*` invariant fns,
  exit-code discipline, `_rows` helper. Re-home as `scripts/probe_endpoints.py` with
  `uw_client.get` replacing the v2 `uw.*` calls.
- `git show e1d6c5e:server/backfill.py` — `_probe_ok`, `_trading_days`, `_has_partition`,
  `_fetch_oi` with `date=` — port to `scripts/probe_oi_depth.py` for the depth binary search.
- `git show e1d6c5e:scripts/extract_golden.py` — golden fixture extraction pattern;
  adapt into `scripts/capture_golden.py` for v3's bronze layout.

---

## Acceptance criteria

- [ ] `tests/fixtures/bronze/` contains a verbatim golden fixture for every endpoint
      Phase 3 needs: `flow-alerts/SPY.json` at minimum; `greek-flow/SPY.json`,
      `net-prem-ticks/SPY.json`, `oi-per-strike/SPY.json` for cross-checks.
- [ ] Each `_FINDING (a)–(e)` block above is filled with the probe command,
      observed result, and resolution. No block left as `<placeholder>`.
- [ ] `scripts/probe_endpoints.py` exits 0 on a healthy UW session (all endpoints OK).
- [ ] `scripts/capture_golden.py` writes atomic bronze fixtures with a `_meta` sidecar.
- [ ] The normalize layer validates the golden fixture (`FlowAlert` round-trip passes).
- [ ] Operator has reviewed and signed off the five findings before Phase 3 begins.

## Definition of done (universal — from the plan)

Typed in/out; provenance on every value; no boundary skipped; REPLAY-reproducible;
golden tests assert a **sane non-None value** (not just field presence).

## Defers to operator

- The operator signs off the five data findings (the empirical results cannot be assumed
  by the implementer — they are observed, recorded, and confirmed before any signal math
  is written).
- Whether to ingest `net-prem-ticks` separately or use it only as the sign cross-check
  (plan §Operator flags #2).
- Whether the "7-day ceiling" in the governor is evidenced or dropped (plan §Operator
  flags #3) — the OI depth probe (d) will inform the lookback claim.

## Open questions / flags

- The 6/5 3:32 PM anchor assumes UW greek-flow retains per-minute history for at least
  48 hours. If the endpoint only returns the current session, an intraday probe on a
  clean-direction day is needed instead. Confirm retention before scripting the sign probe.
- `older_than` pagination for flow-alerts: confirm the parameter name and format (ISO
  timestamp vs epoch ms) from the UW Basic API docs before scripting (c).
- If `net_delta` in net-prem-ticks equals `dir_delta_flow` in greek-flow, the endpoint
  is redundant; operator decides whether to drop it from the Phase 3 ingest list.
