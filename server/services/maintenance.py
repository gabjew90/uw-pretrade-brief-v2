"""Nightly maintenance — in-app scheduler (the Railway volume mounts to ONE service, so
a separate cron service can never see /data; the web container must run its own upkeep).

Runs the three OFFLINE tasks from docs/crons.md at ~03:00 ET daily, sequentially:
backup (tar+sha256) → compact (merge parquet parts) → backtest (append today's re-derived
signal row). ZERO UW calls — this is NOT the forbidden data-refresh loop (basic-platform
rule); it never touches the network or the request path (daemon thread, subprocesses).

State is written to DATA_DIR/maintenance.json and surfaced on /health so the operator can
see the last run from the phone. MAINTENANCE=0 disables; REPLAY skips (offline dev).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from server.config import settings

log = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_RUN_AT = (3, 0)            # 03:00 ET (docs/crons.md)
_TASK_TIMEOUT = 1800        # 30 min per task, generous

TASKS = [
    ("backup", ["scripts/backup_bronze.py"]),
    ("compact", ["scripts/compact.py", "--min-parts", "10", "--include-bronze"]),
    ("backtest", ["scripts/backtest_replay.py", "--ticker", "SPY", "--label", "daily"]),
]


def next_run(now: datetime) -> datetime:
    """The next 03:00 ET strictly after `now` (tz-aware in, tz-aware out)."""
    n = now.astimezone(_ET)
    target = n.replace(hour=_RUN_AT[0], minute=_RUN_AT[1], second=0, microsecond=0)
    if target <= n:
        target += timedelta(days=1)
    return target


def _marker_path() -> Path:
    return settings.data_dir / "maintenance.json"


def run_all(runner=None) -> dict:
    """Run every task sequentially; never raises. Returns + persists the result marker.
    `runner(args) -> (rc, tail)` is injectable for tests; default = subprocess of this
    interpreter from the repo root."""
    repo = Path(__file__).resolve().parent.parent.parent

    def _default(args: list[str]) -> tuple[int, str]:
        p = subprocess.run([sys.executable, *args], cwd=repo, capture_output=True,
                           text=True, timeout=_TASK_TIMEOUT)
        tail = (p.stdout or p.stderr or "").strip().splitlines()[-1:] or [""]
        return p.returncode, tail[0][:200]
    runner = runner or _default

    results = {}
    for name, args in TASKS:
        try:
            rc, tail = runner(args)
            results[name] = {"ok": rc == 0, "note": tail}
        except Exception as e:                      # a task must never kill the loop
            results[name] = {"ok": False, "note": str(e)[:200]}
        log.info("maintenance %s: %s", name, results[name])
    marker = {"last_run": datetime.now(tz=_ET).isoformat(timespec="seconds"),
              "results": results}
    try:
        _marker_path().parent.mkdir(parents=True, exist_ok=True)
        _marker_path().write_text(json.dumps(marker), encoding="utf-8")
    except Exception:                               # marker is best-effort
        log.exception("maintenance marker write failed")
    return marker


def last_run() -> dict | None:
    try:
        return json.loads(_marker_path().read_text(encoding="utf-8"))
    except Exception:
        return None


def _loop() -> None:
    while True:
        wake = next_run(datetime.now(tz=_ET))
        while True:                                 # sleep in chunks (clock drift, restarts)
            remaining = (wake - datetime.now(tz=_ET)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 600))
        run_all()


def start() -> bool:
    """Arm the nightly thread (daemon — never blocks shutdown). No-op in REPLAY or when
    MAINTENANCE=0."""
    if settings.replay or os.getenv("MAINTENANCE", "1").strip() in ("0", "false", "off"):
        return False
    t = threading.Thread(target=_loop, name="maintenance", daemon=True)
    t.start()
    return True
