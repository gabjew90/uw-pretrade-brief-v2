"""Nightly bronze backup (ops-ci spec §5). Bronze is the irreplaceable raw log — UW
history is shallow (flow unrecoverable, OI date= tier-capped), so losing the volume loses
the backtest's past forever.

Tarballs DATA_DIR/bronze with a sha256 manifest. Destination: --out dir (default
DATA_DIR/backups, i.e. the same volume — fine against fat-fingers, NOT against volume
loss; point --out at a mounted bucket, or download the printed file via litterbox/scp
periodically). Standalone: never imports the FastAPI request layer.

    uv run python scripts/backup_bronze.py [--src DIR] [--out DIR] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(settings.bronze))
    ap.add_argument("--out", default=str(settings.data_dir / "backups"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    src = Path(args.src)
    if not src.is_dir():
        print(f"nothing to back up ({src} missing)")
        return 0
    files = sorted(p for p in src.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    print(f"bronze: {len(files)} files, {total/1e6:.1f} MB")
    if args.dry_run:
        print(f"dry-run: would write bronze-{stamp}.tar.gz to {args.out}")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_path = out_dir / f"bronze-{stamp}.tar.gz"
    tmp = tar_path.with_suffix(".gz.tmp")
    with tarfile.open(tmp, "w:gz") as tf:
        tf.add(src, arcname="bronze")
    import os
    os.replace(tmp, tar_path)
    digest = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    (out_dir / f"bronze-{stamp}.sha256").write_text(f"{digest}  {tar_path.name}\n")
    print(f"wrote {tar_path} ({tar_path.stat().st_size/1e6:.1f} MB) sha256={digest[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
