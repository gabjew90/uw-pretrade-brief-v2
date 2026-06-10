"""Re-capture the golden ViewModel snapshot for the REPLAY parity gate (ops-ci spec §4).

Run after any INTENDED ViewModel change (new element, copy edit, field rename), then
commit the updated tests/fixtures/golden_viewmodel/SPY.json. Uses the same harness as the
parity test (tests/replay_harness.py), so setup can never drift between them. Offline —
no UW calls.

    uv run python scripts/capture_golden_vm.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.services import storage  # noqa: E402
from tests.replay_harness import build_replay_vm, seed_lake  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "golden_viewmodel" / "SPY.json"


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        roots = {t: Path(td) / t for t in ("bronze", "silver", "gold")}
        orig = storage._tier_root
        storage._tier_root = lambda tier: roots[tier]
        try:
            seed_lake(roots)
            vm = build_replay_vm().model_dump(mode="json")
        finally:
            storage._tier_root = orig
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vm, indent=2, sort_keys=True), encoding="utf-8")
    print(f"golden ViewModel captured -> {OUT}")
    print(f"  verdict: {vm['verdict']['action']}  elements: {[e['key'] for e in vm['elements']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
