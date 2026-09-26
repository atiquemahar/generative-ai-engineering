# Dockerfile
# Day 46 — Dockerize: Operations Agent API
#
# Two-stage build:
#   base       — Python 3.12-slim + all pip dependencies
#   production — application code, non-root user, health check, uvicorn
#
# Build:
#   docker build -t operations-agent:latest .
#
# Run (standalone):
#   docker run -p 8000:8000 --env-file .env \
#              -v ops_db:/app/data operations-agent:latest
#
# Run (compose):
#   docker compose up --build
#
# Health probe (Docker):
#   curl -f http://localhost:8000/health
#
# Design notes:
#   - Build context is the monorepo root so absolute imports
#     (projects.operations_agent.*) work at runtime.
#   - Only the projects/ directory is copied — experiments/, docs/, *.md,
#     and .env are excluded via .dockerignore.
#   - SQLite DB lives on a named volume (/app/data) so it survives container
#     restarts. Swap DATABASE_URL for PostgreSQL in production.
#   - Non-root appuser prevents container escape via writable /app.

# ── Stage 1: base ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

WORKDIR /app

# curl is needed for the HEALTHCHECK CMD
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies in their own layer so they are cached
# independently of source code changes.
COPY requirements-ops-agent.txt .
RUN pip install --no-cache-dir -r requirements-ops-agent.txt

# ── Stage 2: production ───────────────────────────────────────────────────────
FROM base AS production

# Copy only the application source — no experiments, tests, or secrets.
# (Everything else is excluded by .dockerignore)
COPY projects/ ./projects/

# Persistent SQLite volume mount point.
# Override with DATABASE_URL=postgresql://... for a real DB in production.
RUN mkdir -p /app/data

# Create a non-root user and transfer ownership of the application directory.
# This prevents privilege escalation if a dependency has a vulnerability.
RUN useradd -m appuser \
    && chown -R appuser:appuser /app

USER appuser

# Python must find the repo root (= /app) when resolving absolute imports.
ENV PYTHONPATH="/app"

# Default SQLite DB location (overridden by docker-compose or -e DATABASE_URL).
ENV DATABASE_URL="sqlite:////app/data/operations_agent.db"

EXPOSE 8000

# Docker checks this every 30 s.  The container is marked unhealthy after
# 3 consecutive failures (90 s total), giving uvicorn 30 s to warm up.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run uvicorn from the monorepo root so the projects.* import path resolves.
CMD ["uvicorn", "projects.operations_agent.api.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]