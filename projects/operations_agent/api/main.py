# projects/operations_agent/api/main.py
# Day 46 — Dockerize
#
# FastAPI application for the Operations Agent workflow.
# Entry point for the Docker container:
#
#   CMD ["uvicorn", "projects.operations_agent.api.main:app",
#        "--host", "0.0.0.0", "--port", "8000"]
#
# Lifespan:
#   On startup: creates DB tables (idempotent) and seeds reference data.
#   On shutdown: nothing — SQLAlchemy connection pool closes itself.
#
# Routes:
#   GET  /health                              liveness + DB probe
#   POST /sessions                            start a workflow session
#   GET  /sessions/{thread_id}               read current session state
#   POST /sessions/{thread_id}/messages      send a follow-up message
#   POST /sessions/{thread_id}/approve       approve / reject proposed action
#   GET  /metrics/dashboard                  12 observability metrics (Day 45)

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── Repo root on sys.path ─────────────────────────────────────────────────────
# Required so absolute imports (projects.operations_agent.*) work whether
# uvicorn is launched from /app (Docker) or from the repo root (local dev).
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from projects.operations_agent.database.seed import create_tables, seed_data
from projects.operations_agent.api.routers import health, sessions, metrics

# ── Lifespan (replaces @app.on_event deprecated in FastAPI ≥ 0.93) ───────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup:
      1. create_tables() — Base.metadata.create_all (idempotent, safe to re-run)
      2. seed_data()     — inserts reference customers / orders if empty
 
    Shutdown:
      Nothing explicit — SQLAlchemy pool closes cleanly on process exit.
    """
    create_tables()
    seed_data()
    yield

# ── Application ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Operations Agent API",
    version="1.0.0",
    description=(
        "REST interface for the 12-node LangGraph operations workflow. "
        "Handles customer service requests (refunds, order status, policy queries) "
        "with role-based access control and human-in-the-loop approval."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Allow localhost / frontend dev server during development.
# In production, restrict to your actual frontend origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8080"],
    allow_methods=["*"],
    allow_headers=["*"],   
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(health.router)   # GET  /health
app.include_router(sessions.router, prefix="/sessions")     # POST /sessions, etc.
app.include_router(metrics.router, prefix="/metrics")       # GET  /metrics/dashboard