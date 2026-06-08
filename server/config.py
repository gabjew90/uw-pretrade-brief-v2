"""Runtime configuration — read once from the environment (reusing v2's var names).

Keep this dumb: env in, typed settings out. No secrets are ever logged or written
back. `REPLAY=1` is the offline switch — the whole app must honour it by reading
bronze instead of calling UW (enforced in the governor + uw_client).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # optional: load a local .env in dev (never committed)
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover - dotenv optional
    pass


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # secrets (reused from v2 — same names)
    uw_api_key: str = os.getenv("UW_API_KEY", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    backfill_token: str = os.getenv("BACKFILL_TOKEN", "")
    # storage
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data"))
    # modes
    replay: bool = _flag("REPLAY")
    # budget (governor)
    uw_daily_cap: int = int(os.getenv("UW_DAILY_CAP", "15000") or "15000")
    uw_budget_soft_pct: float = float(os.getenv("UW_BUDGET_SOFT_PCT", "0.9") or "0.9")
    # universe
    ticker_pin_list: tuple[str, ...] = tuple(
        t.strip().upper() for t in os.getenv("TICKER_PIN_LIST", "").split(",") if t.strip()
    )
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    @property
    def bronze(self) -> Path:
        return self.data_dir / "bronze"

    @property
    def silver(self) -> Path:
        return self.data_dir / "silver"

    @property
    def gold(self) -> Path:
        return self.data_dir / "gold"


settings = Settings()
