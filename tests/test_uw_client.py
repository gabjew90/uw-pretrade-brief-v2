"""UW client tests (Phase 1) — hyphenated-path guard, 429 backoff, error paths,
governor gating, header feedback. Network is mocked with `responses`; backoff sleeps
are stubbed; the governor is a fresh isolated instance per test.
"""
import types

import pytest
import responses

from server.services import uw_client
from server.services.governor import Governor, Priority

URL = "https://api.unusualwhales.com/api/option-trades/flow-alerts"
PATH = "/option-trades/flow-alerts"


@pytest.fixture
def env(monkeypatch):
    """Isolate uw_client from real config + the singleton governor; stub backoff sleep."""
    monkeypatch.setattr(uw_client, "settings", types.SimpleNamespace(uw_api_key="test-key"))
    gov = Governor()
    gov.replay = False
    monkeypatch.setattr(uw_client, "governor", gov)
    sleeps: list = []
    monkeypatch.setattr(uw_client.time, "sleep", lambda s: sleeps.append(s))
    return types.SimpleNamespace(gov=gov, sleeps=sleeps)


# ── hyphenated-path guard ────────────────────────────────────────────────────
def test_assert_hyphenated_rejects_underscore():
    with pytest.raises(ValueError):
        uw_client.assert_hyphenated("/option-trades/flow_alerts")
    uw_client.assert_hyphenated("/option-trades/flow-alerts")   # no raise


def test_get_rejects_underscore_before_network(env):
    with pytest.raises(ValueError):
        uw_client.get("/option-trades/flow_alerts")
    assert env.sleeps == []          # failed at the guard, never attempted a call


# ── happy path + header feedback ─────────────────────────────────────────────
@responses.activate
def test_get_success_returns_response_and_feeds_headers(env):
    responses.add(responses.GET, URL, json={"data": []}, status=200,
                  headers={"x-uw-daily-req-count": "7", "x-uw-token-req-limit": "12000"})
    r = uw_client.get(PATH)
    assert r.status == 200
    assert r.json == {"data": []}
    snap = env.gov.snapshot()
    assert snap["source"] == "uw_headers"      # response headers reached the governor
    assert snap["calls_today"] == 7
    assert snap["daily_cap"] == 12000


# ── 429 backoff ──────────────────────────────────────────────────────────────
@responses.activate
def test_get_retries_429_then_succeeds(env):
    responses.add(responses.GET, URL, json={}, status=429)      # first attempt
    responses.add(responses.GET, URL, json={"ok": 1}, status=200)  # retry
    r = uw_client.get(PATH)
    assert r.json == {"ok": 1}
    assert len(env.sleeps) == 1                # backed off exactly once before the retry
    assert env.gov.snapshot()["calls_today"] == 2   # both attempts counted


@responses.activate
def test_get_429_exhausts_raises(env):
    for _ in range(4):                          # max_retries default = 4
        responses.add(responses.GET, URL, json={}, status=429)
    with pytest.raises(uw_client.UWError, match="429 after retries"):
        uw_client.get(PATH)


@responses.activate
def test_non_json_200_raises_uwerror_not_view_killer(env):
    """A 200 with an HTML body (gateway error page) must be a typed UWError so ONE bad
    endpoint degrades to unavailable instead of nuking the whole view (review MOD #4)."""
    responses.add(responses.GET, URL, body="<html>bad gateway</html>", status=200,
                  content_type="text/html")
    with pytest.raises(uw_client.UWError, match="non-JSON"):
        uw_client.get(PATH)


@responses.activate
def test_get_4xx_raises_immediately(env):
    responses.add(responses.GET, URL, json={"err": "nope"}, status=404)
    with pytest.raises(uw_client.UWError, match="HTTP 404"):
        uw_client.get(PATH)
    assert env.sleeps == []                     # 4xx (non-429) is not retried


# ── governor gating ──────────────────────────────────────────────────────────
def test_get_denied_in_replay_raises(env):
    env.gov.replay = True
    with pytest.raises(uw_client.UWError, match="governor denied"):
        uw_client.get(PATH)


def test_get_no_api_key_raises(monkeypatch, env):
    monkeypatch.setattr(uw_client, "settings", types.SimpleNamespace(uw_api_key=""))
    with pytest.raises(uw_client.UWError, match="no UW_API_KEY"):
        uw_client.get(PATH)
