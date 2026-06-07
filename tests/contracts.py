"""UW endpoint schema contracts — the exact fields our code DEPENDS ON.

Each contract lists the per-row fields the parsing code reads (server/uw.py,
server/tile4.py, server/snapshot.py, server/tile3_detail.py). The contract tests
assert these are present on the data rows of golden fixtures (offline) and of
live responses (opt-in `-m live`). "Required-fields-present" strictness: extra
fields UW adds are fine; a MISSING required field is the failure (that's the
drift that silently breaks parsing — e.g. greeks call_delta, atm-chains
straddle-vs-bid/ask, the bugs we hit reactively on 2026-06-01).

`required` = fields that must be on at least the rows we parse. Some endpoints
parse a subset of rows (e.g. greeks rows matching a strike) — the contract checks
the union holds on a representative row, which is enough to catch renames/drops.
"""
from __future__ import annotations

# endpoint -> {"required": [fields], "note": why}
CONTRACTS: dict[str, dict] = {
    "spot_exposures_strike": {
        "required": ["strike", "call_gamma_oi", "put_gamma_oi", "price"],
        "note": "uw.gex_records: net γ = call_gamma_oi + put_gamma_oi; spot from price",
    },
    "spot_exposures_expiry_strike": {
        "required": ["strike", "expiry", "call_gamma_oi", "put_gamma_oi"],
        "note": "tile3_detail: per-expiry OI ladder (call/put gamma per strike)",
    },
    "oi_per_strike": {
        "required": ["strike", "call_oi", "put_oi"],
        "note": "snapshot._extract_oi + read_oi_history: total OI per strike",
    },
    "greeks": {
        "required": ["strike", "call_delta", "put_delta", "call_theta", "put_theta"],
        "note": "tile4: per-strike SEPARATE call_/put_ greeks (NOT flat delta/theta)",
    },
    "atm_chains": {
        "required": ["option_symbol", "bid", "ask", "stock_price"],
        "note": "tile4._expected_move_pct: derive straddle = ATM call mid + put mid "
                "(NO 'straddle' field exists — must compute from bid/ask)",
    },
    "option_contracts": {
        "required": ["option_symbol", "nbbo_bid", "nbbo_ask", "last_price",
                     "implied_volatility", "volume", "open_interest"],
        "note": "uw.contract_records: the chain quote per contract",
    },
    "volatility_term_structure": {
        "required": ["dte", "volatility"],
        "note": "uw.term_structure: IV per expiry (field named 'volatility')",
    },
    "interpolated_iv": {
        "required": ["days"],
        "note": "uw.extract_iv_rank: front-week row via 'days'; percentile/iv_rank "
                "optional (code falls back), so only 'days' is hard-required",
    },
    "greek_exposure_expiry": {
        "required": ["expiry", "dte", "call_charm", "put_charm",
                     "call_vanna", "put_vanna"],
        "note": "tile3_detail: per-expiry charm/vanna drift + dte",
    },
    "greek_flow": {
        "required": ["timestamp", "dir_delta_flow", "total_delta_flow"],
        "note": "greek_flow.build_composite: Tile 1 net-delta composite. PER-MINUTE — "
                "total_delta_flow=headline (net delta traded), dir_delta_flow=conviction lens",
    },
    "earnings": {
        "required": [],   # snapshot._extract_days_to_earnings reads several optional
                          # date keys (report_date/start_date/date) with fallback;
                          # no single field is hard-required. Contract = valid list.
        "note": "_extract_days_to_earnings: any of report_date/start_date/date (optional)",
    },
    "darkpool": {
        "required": ["price", "size"],
        "note": "snapshot._extract_darkpool: net = price * size",
    },
    "flow_alerts": {
        "required": ["ticker", "type", "strike", "total_premium", "all_opening_trades"],
        "note": "snapshot._aggregate_flow + _project_flow_alerts: Tile 1 + flow value; "
                "all_opening_trades drives the opening-flow direction (Ge-Lin-Pearson)",
    },
    "economic_calendar": {
        "required": ["event", "time", "type"],
        "note": "market_regime event veto: upcoming high-impact macro events (type incl 'fomc')",
    },
}


def check_payload(endpoint: str, payload) -> list[str]:
    """Return a list of contract violations for a payload, or [] if it satisfies
    the contract. Checks the FIRST data row (representative) for required fields."""
    spec = CONTRACTS.get(endpoint)
    if spec is None:
        return [f"no contract declared for endpoint '{endpoint}'"]
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return [f"{endpoint}: payload has no 'data' list (got {type(rows).__name__})"]
    if not rows:
        return []  # empty is allowed (e.g. SPY earnings) — not a shape violation
    required = spec["required"]
    if not required:
        return []
    row = rows[0]
    if not isinstance(row, dict):
        return [f"{endpoint}: data rows are not objects"]
    missing = [f for f in required if f not in row]
    return [f"{endpoint}: row missing required field '{f}'" for f in missing]
