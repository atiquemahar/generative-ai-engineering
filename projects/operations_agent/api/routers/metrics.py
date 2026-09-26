# projects/operations_agent/api/routers/metrics.py
# Day 46 — Dockerize
#
# GET /metrics/dashboard
#
# Exposes all 12 DashboardMetrics queries (Day 45) over HTTP.
# Useful for a Grafana data source, a cron report, or a CLI health check.
#
# Optional query param: since (ISO 8601 datetime) scopes queries to recent sessions.
# Example: GET /metrics/dashboard?since=2026-09-01T00:00:00Z

from __future__ import annotations
 
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query

from projects.operations_agent.monitoring.dashboard_metrics import DashboardMetrics

router = APIRouter(tags=["metrics"])

@router.get(
    "/dashboard",
    summary="All 12 observability metrics from audit_logs",
)
def get_dashboard(
    since: Optional[datetime] = Query(
        None,
        description="Scope metrics to sessions after this ISO 8601 UTC timestamp",
        examples="2026-09-01T00:00:00Z",
    ),
) -> dict:
    """
    Returns the full DashboardMetrics snapshot with four categories:
 
    - **latency**: avg/max retrieval and proposal latency in ms
    - **token_usage**: avg/total prompt and completion tokens
    - **operational**: distribution of outcomes, intents, roles, retrieval methods
    - **queried_at**: ISO 8601 UTC timestamp of this snapshot
    """
    return DashboardMetrics().get_all_metrics(since=since)