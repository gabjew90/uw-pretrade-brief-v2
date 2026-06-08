"""UW Pretrade Brief v3 — typed 5-stage pipeline over bronze/silver/gold parquet.

Stage order (never skip a boundary): ingest → normalize → derive → decide → present.
Cross-cutting services: clock, provenance, governor, storage. See docs/architecture.md.
"""
