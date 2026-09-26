# projects/operations_agent/api/routers/health.py
# Day 46 — Dockerize
#
# GET /health
#
# Returns 200 when both the API process and the database are reachable.
# Returns 200 with status="degraded" (not 503) so the Docker HEALTHCHECK
# curl command always gets a response — the degraded flag is for monitoring.
#
# Used by:
#   HEALTHCHECK --interval=30s CMD curl -f http://localhost:8000/health || exit 1

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

from projects.operations_agent.database.engine import SessionLocal
from projects.operations_agent.api.schemas import HealthResponse

router = APIRouter(tags=["health"])

@router.get("/health", response_model=HealthResponse, summary="Liveness + DB check")
def health_check() -> HealthResponse:
    """
    Liveness probe for Docker HEALTHCHECK and load-balancer health checks.
 
    Checks:
      1. API process is alive (trivially true if we reach this handler)
      2. Database is reachable (issues a SELECT 1)
    """
    db_status = "ok"
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:
        db_status = f"error: {exc}" 

    return HealthResponse(
        status= "ok" if db_status=="ok" else "degraded",
        db=db_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )           