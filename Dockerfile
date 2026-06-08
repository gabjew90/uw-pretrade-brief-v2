FROM python:3.11-slim

# uv via pip (smaller than the official installer for our needs)
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy lockfile + pyproject first so `uv sync` is cacheable across code changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Application code
COPY server/ ./server/
COPY static/ ./static/

# Where the parquet archive (bronze/silver/gold) + state land. Railway mounts a volume here.
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

# Honor Railway's $PORT (set dynamically per deploy); fall back to 8000 locally.
CMD ["sh", "-c", "uv run uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
