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

import re
from typing import Callable

from pydantic import ValidationError

from server.models import (FlowAlert, GammaStrike, GreekFlowPoint, IVTermPoint,
                            OISnapshot, OptionContract, SkewPoint)
from server.pipeline.ingest import RawRecord
from server.services import provenance as prov

# OCC-encoded option symbol: SPY260522C00748000 = sym + YYMMDD + C/P + strike*1000
_OCC_RE = re.compile(
    r"^(?P<sym>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<type>[PC])(?P<strike>\d{8})$")


def _parse_occ(symbol: str | None) -> dict | None:
    m = _OCC_RE.match(symbol or "")
    if not m:
        return None
    return {"type": "call" if m["type"] == "C" else "put",
            "expiry": f"20{m['yy']}-{m['mm']}-{m['dd']}",
            "strike": int(m["strike"]) / 1000}

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


def _registry_key(endpoint: str, ticker: str | None = None) -> str:
    """A ticker-AGNOSTIC dispatch key. Per-ticker endpoints carry the symbol in the path
    (`/stock/SPY/greek-flow`); stripping it lets one parser serve every ticker
    (`stock_greek-flow`). Cross-ticker paths (`/option-trades/flow-alerts`) are unaffected."""
    segs = [s for s in endpoint.strip("/").split("/") if s]
    if ticker:
        segs = [s for s in segs if s.upper() != ticker.upper()]
    return "_".join(segs)


def normalize(raw: RawRecord) -> list:
    """Dispatch a raw record to its registered parser. Raises if no parser is
    registered (a missing parser is a wiring error, surfaced — not a silent skip)."""
    key = _registry_key(raw.endpoint, raw.ticker)
    parser = REGISTRY.get(key)
    if parser is None:
        raise NotImplementedError(f"no normalizer registered for endpoint '{key}'")
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


@register("stock_greek-flow")
def normalize_greek_flow(raw: RawRecord) -> list[GreekFlowPoint]:
    """Raw greek-flow payload → validated `list[GreekFlowPoint]`, oldest→newest. Each row
    is PER-MINUTE; `dir_delta_flow` is required (the conviction input, Phase-2 confirmed).
    A degenerate/flat series is NOT an error here — Derive decides `unavailable`; Normalize
    only enforces shape. Empty `{"data":[]}` (pre-market) is a legitimate []."""
    rows = _unwrap(raw.payload)
    p = prov.archive(raw.fetched_at) if raw.from_replay else prov.live(raw.fetched_at)
    out: list[GreekFlowPoint] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise NormalizeError(f"greek-flow row {i} is not an object: {type(r).__name__}")
        try:
            out.append(GreekFlowPoint.model_validate({**r, "provenance": p}))
        except ValidationError as e:
            raise NormalizeError(f"greek-flow row {i} failed validation: {e}") from e
    return out


@register("stock_spot-exposures_strike")
def normalize_spot_exposures(raw: RawRecord) -> list[GammaStrike]:
    """Raw spot-exposures/strike payload → validated `list[GammaStrike]`. Keeps the
    OI-based gamma fields (`call_gamma_oi`, `put_gamma_oi` — UW pre-signs the put
    negative) + the per-row `price` (spot). Derive computes the flip/walls/sign. Empty is
    a legitimate []."""
    rows = _unwrap(raw.payload)
    p = prov.archive(raw.fetched_at) if raw.from_replay else prov.live(raw.fetched_at)
    out: list[GammaStrike] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise NormalizeError(f"spot-exposures row {i} is not an object: {type(r).__name__}")
        try:
            out.append(GammaStrike.model_validate({**r, "provenance": p}))
        except ValidationError as e:
            raise NormalizeError(f"spot-exposures row {i} failed validation: {e}") from e
    return out


@register("stock_historical-risk-reversal-skew")
def normalize_rr_skew(raw: RawRecord) -> list[SkewPoint]:
    """Raw historical-risk-reversal-skew payload → validated `list[SkewPoint]`, date-
    ordered. `risk_reversal` stays in the VENDOR convention here (put_IV − call_IV); Derive
    sign-corrects to call−put. Empty is a legitimate []."""
    rows = _unwrap(raw.payload)
    p = prov.archive(raw.fetched_at) if raw.from_replay else prov.live(raw.fetched_at)
    out: list[SkewPoint] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise NormalizeError(f"rr-skew row {i} is not an object: {type(r).__name__}")
        try:
            out.append(SkewPoint.model_validate({**r, "provenance": p}))
        except ValidationError as e:
            raise NormalizeError(f"rr-skew row {i} failed validation: {e}") from e
    return out


@register("stock_interpolated-iv")
def normalize_interpolated_iv(raw: RawRecord) -> list[IVTermPoint]:
    """Raw interpolated-iv payload → validated `list[IVTermPoint]` (the IV term structure;
    `percentile` is the IV rank the cost gate reads). Empty is a legitimate []."""
    rows = _unwrap(raw.payload)
    p = prov.archive(raw.fetched_at) if raw.from_replay else prov.live(raw.fetched_at)
    out: list[IVTermPoint] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise NormalizeError(f"interpolated-iv row {i} is not an object: {type(r).__name__}")
        try:
            out.append(IVTermPoint.model_validate({**r, "provenance": p}))
        except ValidationError as e:
            raise NormalizeError(f"interpolated-iv row {i} failed validation: {e}") from e
    return out


@register("stock_oi-per-strike")
def normalize_oi_per_strike(raw: RawRecord) -> list[OISnapshot]:
    """Raw oi-per-strike payload → validated `list[OISnapshot]`. `provisional` is stamped
    by the caller (the clock decides if this session's OI has settled — Phase-2 finding:
    intraday OI is FORMING, settled via date=). Empty is a legitimate []."""
    rows = _unwrap(raw.payload)
    p = prov.archive(raw.fetched_at) if raw.from_replay else prov.live(raw.fetched_at)
    out: list[OISnapshot] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise NormalizeError(f"oi-per-strike row {i} is not an object: {type(r).__name__}")
        try:
            out.append(OISnapshot.model_validate({**r, "provenance": p}))
        except ValidationError as e:
            raise NormalizeError(f"oi-per-strike row {i} failed validation: {e}") from e
    return out


@register("stock_option-contracts")
def normalize_option_contracts(raw: RawRecord) -> list[OptionContract]:
    """Raw option-contracts payload → validated `list[OptionContract]`. strike/expiry/type
    parsed from the OCC `option_symbol`; NBBO bid/ask + IV/volume/OI carried. Individual
    unparseable symbols are skipped, but if NONE parse from a non-empty payload that's an
    OCC-format break → `NormalizeError` (never a silently empty chain)."""
    rows = _unwrap(raw.payload)
    p = prov.archive(raw.fetched_at) if raw.from_replay else prov.live(raw.fetched_at)
    out: list[OptionContract] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise NormalizeError(f"option-contracts row {i} is not an object: {type(r).__name__}")
        occ = _parse_occ(r.get("option_symbol"))
        if occ is None:
            continue
        try:
            out.append(OptionContract.model_validate({
                **occ, "bid": r.get("nbbo_bid"), "ask": r.get("nbbo_ask"),
                "iv": r.get("implied_volatility"), "volume": r.get("volume"),
                "open_interest": r.get("open_interest"), "provenance": p}))
        except ValidationError as e:
            raise NormalizeError(f"option-contracts row {i} failed validation: {e}") from e
    if rows and not out:
        raise NormalizeError("option-contracts: no option_symbol parsed (OCC format drift?)")
    return out
