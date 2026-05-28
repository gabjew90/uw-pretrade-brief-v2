# UW Pretrade Brief v2

A weekly-options decision-support dashboard. Hydrates a single static HTML page with live Unusual Whales data + Gemini-generated chart insights. Archives every UW response to a parquet store for longitudinal analytics.

**Live demo:** _set this once Railway URL is generated_

## Architecture

- **FastAPI** server with one background asyncio task that refreshes a snapshot every 60s
- **Storage layer** (`server/storage.py`) wraps every UW call with a 60s-to-5min per-ticker TTL cache; writes every response to `/data/raw/endpoint=.../dt=YYYY-MM-DD/ticker=X/part-HH00.parquet`
- **Tracked universe** (`server/universe.py`) = pinned ∪ indices ∪ sticky ∪ today's hot-15. Fixes the selection bias where rank-driven archives only see "exciting" minutes for a ticker
- **Gemini Flash-Lite** for two chart insights per ticker (structural + curve), 5-min cache
- **Frontend**: the v1 prototype HTML copied byte-for-byte with 6 marker-bracketed edits; a diff test pins the preservation invariant

## Self-host

You need:
- Unusual Whales API key (Basic tier or higher)
- Google Gemini API key (Flash-Lite is fine)
- A Railway account (or any container host)

```bash
# Clone, install
git clone <your-fork-url> uw-pretrade-brief-v2
cd uw-pretrade-brief-v2
uv sync

# Set secrets
cp .env.example .env
# edit .env, fill UW_API_KEY and GEMINI_API_KEY

# Run locally
uv run uvicorn server.main:app --reload
# → http://localhost:8000

# Or via Docker
docker build -t uw-v2 .
docker run -p 8000:8000 -v $(pwd)/data:/data --env-file .env uw-v2
```

## Deploy to Railway

1. Push this repo to GitHub
2. New Railway project → connect repo → Railway autodetects the Dockerfile
3. Add a volume named `data`, mount at `/data`
4. Set env vars: `UW_API_KEY`, `GEMINI_API_KEY`, optionally `TICKER_PIN_LIST`, `REGIME`, `REGIME_DETAIL_TEXT`
5. Deploy. Public URL appears in Railway dashboard.

## License

MIT. Personal use only (UW API tier constraint — don't re-serve UW data to other users).
