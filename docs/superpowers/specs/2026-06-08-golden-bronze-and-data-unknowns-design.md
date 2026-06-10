# Golden Bronze + Data Unknowns — Design (v3, Phase 2)

**Status:** EXECUTED + SIGNED OFF 2026-06-08 — findings (a)–(e) recorded below. (a) sign
PINNED objectively (positive = bullish); (b) operator decision = INGEST net-prem-ticks for
its unique premium/volume-split fields. Phase 2 complete; Phase 3 unblocked.
· **Conforms to:** CLAUDE.md, docs/architecture.md
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
_FINDING (a): greek-flow sign — EXECUTED + PINNED 2026-06-08 (objective, no clean-session needed)
  method: instead of a subjective "clean session", correlate per-minute dir_delta_flow
    against an INDEPENDENT directional measure — net-prem-ticks' UNIQUE fields
    (net_call_premium - net_put_premium). These are different computations from the same
    tape, so positive co-movement pins the sign SENSE binarily.
  data (94 RTH minutes, from captured bronze):
    Pearson r(dir_delta_flow, net_call_prem - net_put_prem) = +0.616
    per-minute sign agreement = 83/94 (88%)
  RESOLUTION (pinned): positive dir_delta_flow = BULLISH / call-side; negative = BEARISH /
    put-side. The +0.62 correlation + 88% sign agreement DECISIVELY rule out the inverse
    convention (which would show ~12% agreement / negative r). Matches the conventional
    expectation; resolves v2's "directional sign not self-validating" caveat.
  caveat (NOT a sign problem): on 6/8 the SESSION SUMS diverge — dir_delta_flow sum = -1.14M
    (net bearish) while net(call-put) premium sum = +19.9M (net bullish), and SPY closed UP
    (~+0.2%, open 743.33 -> 744.91). That intra-day divergence is itself INFORMATIVE (the
    signal-honesty divergence-veto principle), not a calibration error — the per-minute
    co-movement is what pins the sign.
  net_prem_ticks net_delta cross_check: still INVALID (net_delta IS dir_delta_flow, finding
    (b)); the PREMIUM fields (net_call/put_premium) are the valid independent anchor used here.
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
_FINDING (b): net-prem-ticks population — EXECUTED 2026-06-08
  probe_date: 2026-06-08
  net_call_premium populated: yes (e.g. -2,264,372 at 13:30Z)
  net_put_premium populated: yes (e.g. -4,413,000 at 13:30Z)
  net_delta non-degenerate: yes (varies per minute; not uniformly 0)
  series_length_rows: 94 (full RTH minute count, matches greek-flow's 94)
  is_net_delta_same_as_dir_delta_flow: YES — 93/94 minutes byte-identical (the 1 diff is
    the latest minute, a feed-cutoff artifact). net_delta IS greek-flow.dir_delta_flow.
  net-prem-ticks UNIQUE fields (NOT in greek-flow): net_call_premium, net_put_premium,
    net_call_volume, net_put_volume, call_volume_ask_side/bid_side, put_volume_ask_side/bid_side.
  operator_decision (SIGNED OFF 2026-06-08): INGEST net-prem-ticks for its unique
    net_call/put_premium + ask/bid volume split (a directional-premium + aggression read
    distinct from delta flow); do NOT use net_delta as a greek-flow sign cross-check (same
    field). The premium fields are also the valid independent anchor that pinned (a).
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
_FINDING (c): flow-alerts truncation — EXECUTED 2026-06-08
  probe_date: 2026-06-08
  rows_at_limit_500: 500 (at the cap)
  oldest_created_at: 2026-06-05T16:53:37Z
  newest_created_at: 2026-06-08T15:02:42Z
  window_hours: 70.15  (a single 500-row pull spans ~3 calendar days — Fri PM → Mon AM)
  older_than_yields_more: YES (older_than=<oldest> returned 500 MORE)
  resolution: cap = 500 per page; a pull returns the MOST-RECENT N (the tail), which for
    SPY spans multiple sessions; to cover a full/older window, paginate BACKWARD via
    older_than=<oldest created_at>. Tile-1 must stamp the real window + flag truncation
    (carries the v2 flow-alerts-truncation lesson).
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
_FINDING (d): OI live-vs-settled and lookback depth — EXECUTED 2026-06-08 (~11:00 ET)
  probe_date: 2026-06-08  probe_time_et: ~11:00 (RTH)
  intraday_rows (no date=): ~499 (FORMING — differs from settled by value)
  yesterday_rows (date=2026-06-05): 499  (sample call_oi=105)
  same_data_intraday_vs_settled: NO — intraday is forming, not the settled snapshot
  lookback_depth: date=-1d (2026-06-05) OK 499; date=-7d (2026-06-01) OK 498;
    date=-14d (2026-05-22) HTTP 403; -21/-30/-45/-60d ALL HTTP 403
  boundary: works through 2026-06-01 (~6 trading days back); 403 at 2026-05-22 and older
  403_not_empty: the ceiling is a TIER BLOCK (HTTP 403), not an empty 200 — UW refuses
    older dates on Basic. EVIDENCES operator-flag #3: the ~7-trading-day ceiling is REAL.
  settled_field_present: no explicit 'settled' flag observed; clock must gate cadence by
    the OI publish time (~9:15 ET next session), as the v3 clock already does.
  clock_cadence_implication: OI is FORMING intraday; settled OI for a prior session via
    date=; governor lookback bound = measured ~6-7 trading days (requests beyond 403).
  CORRECTION (2026-06-09, operator prompt): the ~7-day ceiling applies to the STOCK-HISTORY
    family only. /option-contract/{id}/historic (accessible category) returns the contract's
    WHOLE LIFE of daily bars under a "chains" root key — probed live: SPY260717P00710000 ->
    61 daily rows, 2026-03-13 through today, each with open_interest + volume + IV + bid/ask
    volume splits. Deep per-contract OI history IS available (scripts/probe_oi_history.py).
    Positioning can trend months of OI on the flow-cluster contracts (~5 calls) instead of
    4 date= sessions.
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
_FINDING (e): hyphenated paths — EXECUTED 2026-06-08
  probe_date: 2026-06-08
  errors: none — all 15 probed endpoints returned 200 (earnings is 200-but-empty: SPY is
    an ETF with no earnings, expected/benign; not a path error)
  underscore_violations: none — every path hyphenated; uw_client.assert_hyphenated() and
    the CI lint (tests/test_uw_paths.py) both pass
  resolution: all 200, all hyphenated. (Note: oi-per-strike older dates return 403 by
    tier, not 404 by path — a budget/lookback limit, not a path bug; see finding (d).)
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
