"""Compaction — merge many small parquet parts per partition into one (ops-ci spec §6).

Append-only writes accumulate a part file per fetch (~15/page-load); DuckDB scans slow
down as parts pile up. This merges every (tier, endpoint, dt, ticker) partition holding
more than --min-parts files into a single part, deleting the originals ONLY after the
merged file is confirmed on disk. Content-preserving (same rows, fewer files); rows are
never modified. Never in the request path — run by cron (see docs/crons.md).

Per the ops-ci spec, bronze is immutable BY POLICY and excluded by default; pass
--include-bronze for a content-preserving merge there too (bronze is where the growth
actually is — operator's call).

    uv run python scripts/compact.py [--min-parts 10] [--include-bronze] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from server.config import settings  # noqa: E402


def compact_partition(part_dir: Path, dry_run: bool) -> int:
    parts = sorted(part_dir.glob("part-*.parquet"))
    if len(parts) < 2:
        return 0
    if dry_run:
        print(f"  would merge {len(parts):>3} parts  {part_dir}")
        return len(parts)
    con = duckdb.connect(database=":memory:")
    try:
        table = con.execute(
            f"SELECT * FROM read_parquet('{part_dir / 'part-*.parquet'}')").fetch_arrow_table()
    finally:
        con.close()
    merged = part_dir / f"part-merged-{parts[-1].name.split('-', 1)[1]}"
    tmp = merged.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp)
    import os
    os.replace(tmp, merged)
    if not merged.exists():                      # belt + suspenders before any unlink
        raise RuntimeError(f"merged part missing: {merged}")
    for p in parts:
        p.unlink()
    print(f"  merged {len(parts):>3} -> 1  {part_dir}")
    return len(parts)


def heal_layout(root: Path, dry_run: bool) -> int:
    """One-time idempotent heal: part files sitting DIRECTLY under a dt= dir (written
    before ticker=_ALL was enforced) break hive reads for the whole endpoint — move them
    into ticker=_ALL/."""
    moved = 0
    for f in root.glob("endpoint=*/dt=*/part-*.parquet"):
        dest = f.parent / "ticker=_ALL" / f.name
        if dry_run:
            print(f"  would move {f} -> ticker=_ALL/")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            import os
            os.replace(f, dest)
        moved += 1
    if moved:
        print(f"  healed {moved} stray part file(s) into ticker=_ALL/")
    return moved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-parts", type=int, default=10)
    ap.add_argument("--include-bronze", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tiers = ["silver", "gold"] + (["bronze"] if args.include_bronze else [])
    for tier in tiers:
        root = {"bronze": settings.bronze, "silver": settings.silver, "gold": settings.gold}[tier]
        if root.is_dir():
            heal_layout(root, args.dry_run)
    total = 0
    for tier in tiers:
        root = {"bronze": settings.bronze, "silver": settings.silver, "gold": settings.gold}[tier]
        if not root.is_dir():
            continue
        print(f"tier {tier}:")
        for part_dir in sorted({p.parent for p in root.rglob("part-*.parquet")}):
            n = len(list(part_dir.glob("part-*.parquet")))
            if n >= args.min_parts:
                total += compact_partition(part_dir, args.dry_run)
    print(f"done, {total} parts {'would be ' if args.dry_run else ''}merged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
