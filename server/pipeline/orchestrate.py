"""Pipeline orchestrator — runs the five stages in order for one ticker.

This is the thin coordinator the HTTP layer calls: ingest → normalize → derive → decide
→ present. It owns NO business logic (each stage does its own job); it only sequences the
typed boundaries and handles the honest-degrade path when a fetch/parse fails.

`assemble()` is factored out (post-ingest) so the pipeline is testable against a captured
`RawRecord` with no network — the REPLAY-reproducibility guarantee: same RawRecord →
identical ViewModel.
"""
from __future__ import annotations

from server.models import ViewModel
from server.pipeline.decide import decide
from server.pipeline.derive import derive_all
from server.pipeline.ingest import RawRecord, ingest
from server.pipeline.normalize import NormalizeError, normalize
from server.pipeline.present import present
from server.services import clock
from server.services.governor import Priority
from server.services.uw_client import UWError

_FLOW_ALERTS = "/option-trades/flow-alerts"


def assemble(ticker: str, flow_raw: RawRecord, *, asof: str | None = None) -> ViewModel:
    """normalize → derive → decide → present over an already-ingested RawRecord. Pure
    given the same RawRecord (REPLAY-reproducible)."""
    alerts = normalize(flow_raw)
    signals = derive_all({"flow_alerts": alerts}, asof=asof)
    verdict = decide(signals)
    return present(ticker, signals, verdict, as_of=asof)


def build_view(ticker: str, *, asof: str | None = None) -> ViewModel:
    """Full pipeline for one ticker. Ingest is governor-gated (live, or bronze in REPLAY/
    over-budget). On a fetch or parse failure we DEGRADE HONESTLY — derive over empty flow
    yields an `unavailable` direction (never a guessed side), surfaced through provenance."""
    ticker = ticker.upper()
    asof = asof or clock.session_date().isoformat()
    try:
        raw = ingest(_FLOW_ALERTS, {"ticker_symbol": ticker, "limit": 500},
                     ticker=ticker, priority=Priority.CRITICAL)
        return assemble(ticker, raw, asof=asof)
    except (UWError, NormalizeError):
        signals = derive_all({"flow_alerts": []}, asof=asof)   # honest-degrade
        verdict = decide(signals)
        return present(ticker, signals, verdict, as_of=asof)
