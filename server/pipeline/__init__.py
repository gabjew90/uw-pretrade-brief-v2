"""The 5-stage pipeline. Each stage consumes ONLY the previous stage's typed output:

    ingest → normalize → derive → decide → present
    (raw)    (canonical)  (signals) (verdict) (view model)

No stage skips a boundary. No stage does another stage's job (the v2 failure: one
1,248-line builder doing all five). Cross-cutting services (clock, provenance,
governor, storage) are injected, never reached around.
"""
