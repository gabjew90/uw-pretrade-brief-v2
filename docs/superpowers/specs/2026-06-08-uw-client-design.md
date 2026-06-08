# UW Client — Design (v3, Phase 1)

**Status:** PLAN (awaiting approval) · **Conforms to:** CLAUDE.md, docs/architecture.md
**Depends on:** `server.services.governor` (budget gate), `server.config` (API key)
**Starting point:** `server/services/uw_client.py`

## Purpose / role in the pipeline

The one and only module that touches the UW network. No other module may call
`requests.get` against `api.unusualwhales.com`. This boundary makes the hyphenated-path
lint, the 429 backoff, the governor gate, and the header-feedback loop checkable in one
place, and it makes the Ingest stage testable by swapping this module.

## Contract (typed in/out — reference server.models + the contracts spec)

```python
# A successful fetch
@dataclass
class UWResponse:
    endpoint:   str          # the path as passed (e.g. "/option-trades/flow-alerts")
    params:     dict
    status:     int
    json:       dict
    fetched_at: str          # ISO-8601 UTC — used by Ingest as RawRecord.fetched_at

# A non-recoverable failure (budget, auth, network, exhausted retries)
class UWError(Exception): ...

# The one public entry point
def get(path: str, params: dict | None = None, *,
        priority: Priority = Priority.NORMAL,
        max_retries: int = 4) -> UWResponse:  # raises UWError; never returns None

# Path discipline (also enforced at CI via tests/test_uw_paths.py)
def assert_hyphenated(path: str) -> None:   # raises ValueError on underscores in resource segments
```

## Responsibilities (and explicit NON-responsibilities)

**Owns:**
- `assert_hyphenated`: called at the top of `get()`. Raises `ValueError` immediately on
  any path segment containing `_` (UW's API uses hyphens; underscores 404 silently).
  Also lintable at CI as a static check over all call sites.
- Governor gate: `governor.check(priority=priority)` before any network call. If denied,
  raises `UWError(f"governor denied: {decision.reason}")` — the caller converts to
  `provenance.unavailable(...)`.
- HTTP execution: `requests.get(url, params, headers, timeout=_TIMEOUT)` — sync,
  single-threaded. Callers that want parallelism wrap calls in a `ThreadPoolExecutor`.
  Do NOT add `httpx`, `aiohttp`, or `async def` here; the binding constraint is budget,
  not I/O concurrency.
- `governor.record(1)` after every HTTP attempt, including retries.
- `governor.update_from_headers(r.headers)` after every successful (`2xx`) response.
- 429 backoff: exponential with jitter, up to `max_retries` attempts. On exhaustion,
  raises `UWError("429 after retries")`.
- 4xx (non-429) / 5xx: raises `UWError(f"HTTP {status} for {path}")` immediately (no
  retry — a 404 is a caller bug, a 403 is an auth bug; neither improves with retries).
- Network errors (`requests.RequestException`): retry up to `max_retries`; on
  exhaustion raises `UWError(f"network: {e}")`.
- No API key set: raises `UWError("no UW_API_KEY set")` before any network attempt.
- `fetched_at`: `datetime.now(timezone.utc).isoformat()` captured after a successful
  response, before returning. This is what Ingest stores in `RawRecord.fetched_at`.

**Does NOT own:**
- Parsing or validating the response JSON — that is Normalize's job.
- Storing the response — that is Ingest's job (Ingest calls `uw_client.get`, then
  `storage.write_part`).
- Retry-on-404 — a 404 means the path is wrong (almost certainly an underscore bug);
  `assert_hyphenated` should catch it before the call.
- Any UW endpoint path constants — those live in the Ingest pipeline module that calls
  `get()`. The client is path-agnostic.

## Key behaviors / edge cases

- **Underscore lint** is the first line of `get()` — a bad path raises `ValueError`
  before the governor is even consulted. This means a path bug surfaces as a test
  failure or a startup error, never as a silent cache miss that looks like "no data."
- **Governor first**: if the governor denies (REPLAY, cap, rate), the error is raised
  before any socket is opened. No token consumed.
- **`governor.record(1)` on every attempt**: retries consume budget quota. On a 429
  retry sequence, each `requests.get` call records 1. This is consistent with v2's
  intent (`budget.record_call()` inside the retry loop).
- **Header feedback after 2xx only**: `update_from_headers` is not called on 429 or
  error responses because UW's authoritative daily count in headers is only reliable
  on successful responses.
- **Backoff with jitter**: initial delay 0.6 s, doubles each retry, plus random jitter
  in [0, 0.1 * delay]. Jitter prevents thundering herd when multiple in-flight calls
  all hit 429 simultaneously.
- **`max_retries` default 4**: gives delays of ~0.6, 1.2, 2.4, 4.8 s before giving up
  (~9 s total). Operator may lower for latency-sensitive CRITICAL paths.
- **Thread safety**: `get()` is stateless (all state lives in `governor`). Multiple
  threads calling `get()` concurrently is safe; governor's lock protects the meter.

## Keepers to port from v2 (`git show e1d6c5e:server/uw.py`)

| v2 item | Where it lands in v3 |
|---|---|
| `_429_RETRY_DELAYS_S = (1, 2)` exponential backoff | Extended to `max_retries=4` with computed delay (`delay *= 2`); jitter added |
| `budget.record_call()` inside retry loop | `governor.record(1)` after each `requests.get` attempt |
| `budget.record_usage_headers(r.headers)` on every response | `governor.update_from_headers(r.headers)` on 2xx only |
| `_rate_limit_hint(headers)` diagnostic string on 429 | Retained as a private helper; folded into `UWError` message on exhaustion |
| `"UW-CLIENT-API-ID": "100001"` required header | Kept in `headers` dict; value from v2 |
| `BASE = "https://api.unusualwhales.com"`, `TIMEOUT_S = 5` | `_BASE` / `_TIMEOUT` constants; timeout deferred to operator (5 s may be too short for large payloads) |
| Endpoint method functions (`fetch_spot_exposures_strike`, `fetch_oi_strike`, etc.) | **Not ported**: path constants and parameter construction move into the Ingest pipeline module. The client is path-agnostic. |
| `_FLOW_ALERTS_MAX = 500` note about silent fallback | Documented as an Ingest-layer concern; not a client constant |

## Acceptance criteria

- [ ] `assert_hyphenated("/api/stock/SPY/oi-per-strike")` → no error.
- [ ] `assert_hyphenated("/api/stock/SPY/oi_per_strike")` → `ValueError`.
- [ ] `get(...)` with no `UW_API_KEY` set → `UWError` raised before any socket.
- [ ] `get(...)` when governor denies → `UWError("governor denied: ...")` raised before any socket.
- [ ] `get(...)` on 429 response → retries up to `max_retries`; `governor.record(1)` called once per attempt.
- [ ] `get(...)` on 429 exhaustion → `UWError("429 after retries")`.
- [ ] `get(...)` on HTTP 404 → `UWError("HTTP 404 for ...")` immediately (no retry).
- [ ] `get(...)` on 200 → `UWResponse` with `fetched_at` in UTC ISO-8601; `governor.update_from_headers` called once.
- [ ] `get(...)` on network error → retries; exhaustion → `UWError("network: ...")`.
- [ ] `tests/test_uw_paths.py` hyphen-lint passes over all Ingest call sites.
- [ ] REPLAY mode: `governor.check()` denies → `UWError` raised; no `requests.get` call made (verifiable by mocking).

## Definition of done

Typed in/out · provenance on every value (UWError → caller marks `unavailable`;
UWResponse → Ingest attaches `live` provenance with `fetched_at`) · no boundary
skipped · REPLAY-reproducible (governor enforces replay; client never called in REPLAY).

## Defers to operator

- `_TIMEOUT` (currently 5 s; may need raising for large strike-chain responses).
- `max_retries` default (currently 4).
- Backoff base delay and jitter factor.
- `UW-CLIENT-API-ID` header value (currently `"100001"` from v2; confirm with UW docs).

## Open questions / flags

- **`UW-CLIENT-API-ID` header**: v2 added this citing "anti-hallucination protocol" in
  comments. Confirm with UW API documentation whether this header is required, optional,
  or ignored for Basic-tier keys. If undocumented, flag for operator to probe.
- **Timeout 5 s**: v2's `TIMEOUT_S = 5` was appropriate for small payloads. The
  `spot-exposures` endpoint with `limit=500` may exceed this. Recommend: raise to 20 s
  (already in scaffold) or make it per-call configurable. Defer to operator.
- **Retry on network error vs 429**: currently both retry with the same backoff. A
  network error might warrant immediate fail-fast (the load balancer is down; retrying
  wastes time). Recommend: keep retrying for now; flag for operator to tune.
