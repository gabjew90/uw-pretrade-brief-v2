"""Replay-as-backtest — re-derive signal history from immutable bronze (ops-ci spec §7).

Because bronze is the verbatim raw log and derive/decide are pure, replaying each
archived session through the CURRENT code reconstructs what every signal and the verdict
WOULD have said that day — with zero UW calls. Output is a line-diffable
signal_history.jsonl in gold/backtest/<label>/ (a hypothesis namespace, never production
gold). This is the learning loop: "what did the tool say each day", and the harness for
validating thresholds before changing them.

    uv run python scripts/backtest_replay.py --ticker SPY [--label run1]

Coverage note: a session re-derives from whatever bronze exists for that date — endpoints
not archived that day degrade to unavailable signals, exactly like live honest-degrade.
The archive only grows forward (UW history is shallow), so the harness gets stronger
every trading day the dashboard is used.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import settings  # noqa: E402
from server.pipeline.derive import flow_side, session_alerts  # noqa: E402
from server.pipeline.ingest import RawRecord  # noqa: E402
from server.pipeline.normalize import normalize  # noqa: E402
from server.pipeline.orchestrate import _flow_cluster, assemble_from_canon  # noqa: E402
from server.services import storage  # noqa: E402
from datetime import date  # noqa: E402


def _latest_per_dt(endpoint_key: str, ticker: str | None) -> dict[str, dict]:
    """{dt: latest bronze row} for one endpoint partition (hive `dt` column)."""
    where, params = ("ticker = ?", [ticker.upper()]) if ticker else ("", None)
    rows = storage.read_endpoint("bronze", endpoint_key, where=where, params=params)
    by_dt: dict[str, dict] = {}
    for r in rows:
        dt = str(r.get("dt") or "")
        if dt and (dt not in by_dt or r.get("fetched_at", "") > by_dt[dt].get("fetched_at", "")):
            by_dt[dt] = r
    return by_dt


def _raw(endpoint: str, ticker: str, row: dict) -> RawRecord:
    return RawRecord(endpoint=endpoint, params={}, ticker=ticker,
                     fetched_at=row.get("fetched_at", ""), content_hash=row.get("content_hash", ""),
                     payload=json.loads(row["response"]), from_replay=True)


def backtest(ticker: str) -> list[dict]:
    t = ticker.upper()
    eps = {
        "flow": ("option-trades_flow-alerts", "/option-trades/flow-alerts"),
        "greek_flow": (f"stock_{t}_greek-flow", f"/stock/{t}/greek-flow"),
        "gamma": (f"stock_{t}_spot-exposures_strike", f"/stock/{t}/spot-exposures/strike"),
        "skew": (f"stock_{t}_historical-risk-reversal-skew", f"/stock/{t}/historical-risk-reversal-skew"),
        "iv": (f"stock_{t}_interpolated-iv", f"/stock/{t}/interpolated-iv"),
        "chain": (f"stock_{t}_option-contracts", f"/stock/{t}/option-contracts"),
        "term": (f"stock_{t}_volatility_term-structure", f"/stock/{t}/volatility/term-structure"),
    }
    per_dt = {name: _latest_per_dt(key, t if name != "flow" else t)
              for name, (key, _) in eps.items()}
    sessions = sorted(per_dt["flow"].keys())
    out: list[dict] = []
    for dt in sessions:
        canon: dict = {}
        for name, (key, path) in eps.items():
            row = per_dt[name].get(dt)
            if not row:
                continue
            try:
                recs = normalize(_raw(path, t, row))
            except Exception:
                continue
            canon[{"flow": "flow_alerts", "greek_flow": "greek_flow", "gamma": "gamma_strikes",
                   "skew": "skew_rr", "iv": "iv_term", "chain": "option_contracts",
                   "term": "term_structure"}[name]] = recs
        alerts = canon.get("flow_alerts") or []
        if not alerts:
            continue
        sess = session_alerts(alerts)
        side, _ = flow_side(sess)
        canon["spot"] = next((g.price for g in canon.get("gamma_strikes") or [] if g.price), None)
        if side:
            canon["flow_side"] = side
            canon["flow_strikes"] = _flow_cluster(sess, side, date.fromisoformat(dt))
        vm = assemble_from_canon(t, canon, asof=dt)
        sig = {e.key: e.surface for e in vm.elements}
        out.append({"date": dt, "ticker": t, "action": vm.verdict.action,
                    "overall": vm.verdict.overall, "direction": vm.verdict.direction,
                    "reasons": vm.verdict.reasons, "surfaces": sig})

    # OUTCOME JOIN — "was it right": next archived session's close move, from bronze
    # stock-state (zero UW calls; coverage matches the flow bronze by construction).
    closes: dict[str, float] = {}
    for dt, row in _latest_per_dt(f"stock_{t}_stock-state", t).items():
        try:
            data = json.loads(row["response"]).get("data") or {}
            d0 = data[0] if isinstance(data, list) else data
            v = float(d0.get("close") or d0.get("prev_close") or 0)
            if v > 0:
                closes[dt] = v
        except Exception:
            continue
    dts = sorted(closes)
    for r in out:
        nxt = next((d for d in dts if d > r["date"]), None)
        if r["date"] in closes and nxt:
            pct = (closes[nxt] - closes[r["date"]]) / closes[r["date"]] * 100
            r["outcome_date"], r["outcome_pct"] = nxt, round(pct, 2)
            r["called_right"] = ((pct > 0) if r["direction"] == "calls" else
                                 (pct < 0) if r["direction"] == "puts" else None)
        else:
            r["outcome_date"] = r["outcome_pct"] = r["called_right"] = None
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="SPY")
    ap.add_argument("--label", default="run")
    args = ap.parse_args()
    rows = backtest(args.ticker)
    out_dir = settings.gold / "backtest" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "signal_history.jsonl"
    # REWRITE (not append): the file is fully regenerable from bronze each run, which
    # keeps it deduped and lets outcomes back-fill as later sessions arrive.
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    print(f"{len(rows)} session(s) re-derived -> {out}")
    for r in rows:
        print(f"  {r['date']}  {r['overall']:<10}  {r['action']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
