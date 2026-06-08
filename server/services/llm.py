"""LLM insights (Gemini Flash-Lite) — OPTIONAL narration over the view model.

Insights are presentation garnish, never a signal: they narrate what Derive/Decide
already produced. With no GEMINI_API_KEY the caller falls back to deterministic
templates. This is a stub — wired per instructions in the Present stage.
"""
from __future__ import annotations

from server.config import settings


def available() -> bool:
    return bool(settings.gemini_api_key)


def narrate(prompt: str, facts: dict) -> str:
    """Return a one-paragraph plain-English read. Stub: returns "" until wired so the
    caller uses its deterministic fallback. NEVER invents numbers not in `facts`."""
    return ""
