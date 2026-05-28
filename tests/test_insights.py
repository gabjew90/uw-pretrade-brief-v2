"""Tests for the Gemini wrapper, 5-min cache, and deterministic fallback."""
from __future__ import annotations
from unittest.mock import MagicMock
import pytest

from server import insights


def _row(**overrides):
    base = {
        "ticker": "NVDA",
        "direction": "calls",
        "spot": 154.20,
        "gates": {"structural": "green", "cost": "green"},
        "flip_dist_pct": -1.2,
        "wall_up_dist_pct": 3.5,
        "wall_dn_dist_pct": 2.1,
        "iv_term_curve": [0.38, 0.41, 0.43, 0.45],
    }
    base.update(overrides)
    return base


@pytest.fixture
def fresh_insight_cache():
    insights._insight_cache._store.clear()
    yield
    insights._insight_cache._store.clear()


def test_fallback_structural_text_when_gemini_disabled(fresh_insight_cache, monkeypatch):
    """No GEMINI_API_KEY → deterministic rules-based fallback fires."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = insights.generate_insights(_row())
    assert "structural" in out and "curve" in out
    assert "spot" in out["structural"].lower() or "flip" in out["structural"].lower()


def test_gemini_path_called_when_key_present(fresh_insight_cache, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_client = MagicMock()
    mock_client.generate.return_value = "MOCK_INSIGHT"
    monkeypatch.setattr(insights, "_get_client", lambda: mock_client)

    out = insights.generate_insights(_row())
    assert out["structural"] == "MOCK_INSIGHT"
    assert out["curve"] == "MOCK_INSIGHT"
    assert mock_client.generate.call_count == 2


def test_cache_hit_within_5_min_skips_gemini(fresh_insight_cache, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_client = MagicMock()
    mock_client.generate.return_value = "MOCK"
    monkeypatch.setattr(insights, "_get_client", lambda: mock_client)

    insights.generate_insights(_row())
    insights.generate_insights(_row())
    assert mock_client.generate.call_count == 2, "second call must hit cache, not Gemini"


def test_cache_key_quantizes_flip_pct_to_half_pct_buckets(fresh_insight_cache, monkeypatch):
    """flip_dist_pct of -1.1 and -1.0 share a cache key (both round to -1.0)."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_client = MagicMock()
    mock_client.generate.return_value = "MOCK"
    monkeypatch.setattr(insights, "_get_client", lambda: mock_client)

    insights.generate_insights(_row(flip_dist_pct=-1.1))
    mock_client.generate.reset_mock()
    insights.generate_insights(_row(flip_dist_pct=-1.0))
    # both round to -1.0 → cache hit on the 2nd call → no Gemini call
    assert mock_client.generate.call_count == 0


def test_gemini_failure_falls_back_to_rules_based(fresh_insight_cache, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_client = MagicMock()
    mock_client.generate.side_effect = RuntimeError("Gemini 5xx")
    monkeypatch.setattr(insights, "_get_client", lambda: mock_client)

    out = insights.generate_insights(_row())
    assert out["structural"] is not None
    assert "Gemini" not in out["structural"]
