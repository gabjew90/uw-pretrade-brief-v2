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
