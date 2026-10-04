# projects/operations_agent/api/main.py
# Day 46 — Dockerize
#
# FastAPI application for the Operations Agent workflow.
# Entry point for the Docker container:
#
#   CMD ["uvicorn", "projects.operations_agent.api.main:app",
#        "--host", "0.0.0.0", "--port", "8000"]
#
# Lifespan (Day 48 change):
#   On startup:
#     1. get_settings()    — validates ALL required env vars are present.
#                            Raises ValueError immediately if any are missing.
#                            The container fails its HEALTHCHECK → ACA marks it
#                            unhealthy → traffic is never routed to it.
#                            "Fail fast at startup" beats "fail silently at 3am."
#     2. create_tables()   — Base.metadata.create_all (idempotent)
#     3. seed_data()       — inserts reference data if tables are empty
#     4. Logs redacted_repr() — shows SET/MISSING per var, never actual values.
#   On shutdown:
#     Nothing — SQLAlchemy pool closes cleanly on process exit.
#
# Secret flow in production (Day 48):
#   Key Vault secret → ACA Key Vault reference → ACA env var → os.environ
#   Application sees normal env vars; no SDK change needed in any tool or node.
#
# Routes:
#   GET  /health                              liveness + DB probe
#   POST /sessions                            start a workflow session
#   GET  /sessions/{thread_id}               read current session state
#   POST /sessions/{thread_id}/messages      send a follow-up message
#   POST /sessions/{thread_id}/approve       approve / reject proposed action
#   GET  /metrics/dashboard                  12 observability metrics (Day 45)

from __future__ import annotations
import logging
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

from projects.operations_agent.config import get_settings           # Day 48
from projects.operations_agent.database.seed import create_tables, seed_data
from projects.operations_agent.api.routers import health, sessions, metrics

logger = logging.getLogger(__name__)

# ── Lifespan (replaces @app.on_event deprecated in FastAPI ≥ 0.93) ───────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
        Startup (Day 48 order matters):
    
          Step 1 — get_settings()
            Calls Settings.from_env() on first invocation.
            If ANY required env var is missing (e.g. a Key Vault reference
            failed to resolve), ValueError is raised here — before any request
            is served.  The HEALTHCHECK curl -f http://localhost:8000/health
            will never succeed, so ACA keeps the revision unhealthy and rolls
            back automatically.
    
            Log output (safe — no actual secret values):
              "Settings loaded: {'AZURE_OPENAI_API_KEY': 'SET', ...}"
    
          Step 2 — create_tables() / seed_data()
            Idempotent DB setup — unchanged from Day 46.
    
        Shutdown:
          Nothing explicit — SQLAlchemy pool closes cleanly on process exit.
    """
    # ── Step 1: fail fast if any required env var is missing ─────────────────
    settings = get_settings()                       # raises ValueError on missing var
    logger.info("Settings loaded: %s", settings.redacted_repr())
    
        # ── Step 2: DB setup (unchanged from Day 46) ──────────────────────────────
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