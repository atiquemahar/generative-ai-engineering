# projects/operations_agent/monitoring/dashboard_metrics.py
# Day 45 — Foundry Tracing + Observability
#
# DashboardMetrics: queries audit_logs to produce the 12 metrics
# that populate the Foundry observability dashboard.
#
# 12 Metrics — 4 per category:
#
#   Latency (4):
#     1. avg_retrieval_latency_ms          avg of policy_latency_ms
#     2. max_retrieval_latency_ms          worst-case retrieval latency
#     3. avg_propose_action_latency_ms     avg of propose_action_latency_ms
#     4. max_propose_action_latency_ms     worst-case proposal latency
#
#   Token usage (4):
#     5. avg_propose_input_tokens          avg prompt tokens per LLM call
#     6. avg_propose_output_tokens         avg completion tokens per LLM call
#     7. total_propose_input_tokens        cumulative prompt tokens (billing proxy)
#     8. total_propose_output_tokens       cumulative completion tokens (billing proxy)
#
#   Business / operational (4):
#     9.  execution_outcome_distribution   {outcome: count}
#     10. request_volume_by_intent         {intent: count}
#     11. role_distribution                {role: count}
#     12. retrieval_method_distribution    {policy_retrieval_method: count}
#
# Data foundation:
#   - Metrics 1-4 require the new tracing columns added on Day 45
#     (policy_latency_ms, propose_action_latency_ms).
#     Rows written before Day 45 will have NULL for these columns;
#     the averages and maxima silently skip NULLs (SQL AVG/MAX behaviour).
#   - Metrics 5-8 require propose_input_tokens / propose_output_tokens columns.
#   - Metrics 9-12 use columns that exist since Day 44.
#
# Usage:
#   from projects.operations_agent.monitoring.dashboard_metrics import DashboardMetrics
#
#   snapshot = DashboardMetrics().get_all_metrics()
#   print(snapshot["latency"]["avg_retrieval_latency_ms"])
#
# Each metric method also accepts an optional `since` datetime to scope
# the query to recent sessions only.

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from projects.operations_agent.database.engine import SessionLocal
from projects.operations_agent.database.models import AuditLog

logger = logging.getLogger(__name__)

class DashboardMetrics:
    """
    Queries audit_logs to produce the 12 Foundry dashboard metrics.
 
    Instantiate once per query cycle (or reuse — it holds no open connections).
    All queries are read-only SELECT statements.
    """

    # ── Latency metrics ───────────────────────────────────────────────────────

    def avg_retrieval_latency_ms(self, since: Optional[datetime] = None) -> Optional[float]:
        """
        Average time KnowledgeAgent.ask() took to answer a policy query.
        Source column: audit_logs.policy_latency_ms (Day 45).
        Returns None when no rows with latency data exist.
        """
        with SessionLocal() as session:
            q = select(func.avg(AuditLog.policy_latency_ms)).where(
                AuditLog.policy_latency_ms.isnot(None)
            )
            if since:
                q = q.where(AuditLog.timestamp >= since)
            result = session.execute(q).scalar()
        return round(float(result), 2)  if result is not None else None

    def max_retieval_latency_ms(self, since: Optional[datetime] = None) -> Optional[float]:
        """Maximum single-request retrieval latency — useful for SLO alerting."""
        with SessionLocal() as session:
            q = select(func.max(AuditLog.policy_latency_ms)).where(
                AuditLog.policy_latency_ms.isnot(None)
            )
            if since:
                q = q.where(AuditLog.timestamp >=since)  
            result = session.execute(q).scalar()
        return round(float(result), 2) if result is not None else None

    def avg_propose_action_latency_ms(self, since: Optional[datetime] = None) -> Optional[float]:
        """
        Average time the LLM took to produce a proposed action (propose_action node).
        Source column: audit_logs.propose_action_latency_ms (Day 45).
        """
        with SessionLocal() as session:
            q = select(func.avg(AuditLog.propose_action_latency_ms)).where(
                AuditLog.propose_action_latency_ms.isnot(None)
            )
            if since:
                q = q.where(AuditLog.timestamp >= since) 
            result = session.execute(q).scalar()
        return round(float(result), 2) if result is not None else None

    def max_propose_action_latency_ms(self, since: Optional[datetime] =None) -> Optional[float]:
        """Maximum single-request proposal latency — useful for SLO alerting."""
        with SessionLocal() as session:
            q = select(func.max(AuditLog.propose_action_latency_ms)).where(
                AuditLog.propose_action_latency_ms.isnot(None)
            )
            if since:
                q = q.where(AuditLog.timestamp >= since)
            result = session.execute(q).scalar()
        return round(float(result), 2) if result is not None else None

    def avg_propose_input_tokens( self, since: Optional[datetime] = None) -> Optional[float]:
        """
        Average prompt token count per propose_action LLM call.
        Grows with context window (longer conversation → more tokens).
        """
        with SessionLocal() as session:
            q = select(func.avg(AuditLog.propose_input_tokens)).where(
                AuditLog.propose_input_tokens.isnot(None)
            )
            if since:
                q = q.where(AuditLog.timestamp >= since)
            result = session.execute(q).scalar()
        return round(float(result), 1) if result is not None else None

    def avg_propose_output_tokens(self, since: Optional[datetime] = None) -> Optional[float]:
        """Average completion token count per propose_action LLM call."""
        with SessionLocal() as session:
            q = select(func.avg(AuditLog.propose_output_tokens)).where(
                AuditLog.propose_output_tokens.isnot(None)
            )
            if since:
                q = q.where(AuditLog.timestamp >= since)
            result = session.execute(q).scalar()
        return round(float(result), 1) if result is not None else None

    def total_propose_input_tokens(self, since: Optional[datetime] = None) -> int:
        """
        Cumulative prompt tokens across all logged sessions.
        Multiply by your per-token cost for a billing proxy.
        """
        with SessionLocal() as session:
            q = select(func.sum(AuditLog.propose_input_tokens)).where(
                AuditLog.propose_input_tokens.isnot(None)
            )
            if since:
                q = q.where(AuditLog.timestamp >= since)
            result = session.execute(q).scalar()
        return int(result) if result is not None else 0  

    def total_propose_output_tokens(self, since: Optional[datetime] = None) -> int:
        """
        Cumulative completion tokens across all logged sessions.
        Output tokens are typically billed at a higher rate than input tokens.
        """
        with SessionLocal() as session:
            q = select(func.sum(AuditLog.propose_output_tokens)).where(
                AuditLog.propose_output_tokens.isnot(None)
            )
            if since:
                q = q.where(AuditLog.timestamp >= since)
            result = session.execute(q).scalar()
        return int(result) if result is not None else 0  

    # ── Business / operational metrics ────────────────────────────────────────
 
    def execution_outcome_distribution(self, since: Optional[datetime] = None) -> dict[str, int]:
        """
        Count of each execution_outcome value.
        Reveals the mix of: success, rejected, ineligible, role_denied,
        duplicate_skipped, inform_only, and failure variants.
        """
        return self._distribution(AuditLog.execution_outcome, since) 

    def request_volume_by_intent(self, since: Optional[datetime] = None) -> dict[str, int]:
        """
        Count of requests per intent.
        Reveals which workflows are most used (refund vs order_status vs …).
        """
        return self._distribution(AuditLog.intent, since) 

    def role_distribution(self, since: Optional[datetime] = None) -> dict[str, int]:
        """
        Count of requests per user_role.
        Useful for access-pattern auditing and capacity planning per role tier.
        """
        return self._distribution(AuditLog.user_role, since)  

    def retrieval_method_distribution(self, since: Optional[datetime] = None) -> dict[str, int]:
        """
        Count of requests per policy_retrieval_method.
        Reveals how often hybrid_semantic vs keyword vs fallback was used.
        Links latency to method: is the fast method being selected?
        """
        return self._distribution(AuditLog.policy_retrieval_method, since)  

    # ── Aggregated snapshot ───────────────────────────────────────────────────
    def get_all_metrics(self, since: Optional[datetime] = None) -> dict:
        """
        Run all 12 metrics in one call and return a structured snapshot.
 
        Structure:
            {
                "latency": {
                    "avg_retrieval_latency_ms":          float | None,
                    "max_retrieval_latency_ms":          float | None,
                    "avg_propose_action_latency_ms":     float | None,
                    "max_propose_action_latency_ms":     float | None,
                },
                "token_usage": {
                    "avg_propose_input_tokens":          float | None,
                    "avg_propose_output_tokens":         float | None,
                    "total_propose_input_tokens":        int,
                    "total_propose_output_tokens":       int,
                },
                "operational": {
                    "execution_outcome_distribution":    dict[str, int],
                    "request_volume_by_intent":          dict[str, int],
                    "role_distribution":                 dict[str, int],
                    "retrieval_method_distribution":     dict[str, int],
                },
                "queried_at": str (ISO 8601),
                "since":      str | None,
            }
 
        The four top-level keys map directly to the four metric categories
        in the Day 45 roadmap.
        """
        snapshot = {
            "latency": {
                "avg_retrieval_latency_ms": self.avg_retrieval_latency_ms(since),
                "max_retrieval_latency_ms": self.max_retieval_latency_ms(since),
                "avg_action_propose_action_latency_ms": self.avg_propose_action_latency_ms(since),
                "max_propose_action_latency_ms": self.max_propose_action_latency_ms(since),
            },
            "token_usage": {
                "avg_propose_input_tokens": self.avg_propose_input_tokens(since),
                "avg_propose_output_tokens": self.avg_propose_output_tokens(since),
                "total_propose_input_tokens": self.total_propose_input_tokens(since),
                "total_propose_output_tokens": self.total_propose_output_tokens(since),
            },
            "operational": {
                "execute_outcome_distribution": self.execution_outcome_distribution(since),
                "request_volume_by_intent": self.request_volume_by_intent(since),
                "role_distribution": self.role_distribution(since),
                "retrieval_method_distribution": self.retrieval_method_distribution(since),
            },
            "queried_at": datetime.now(timezone.utc).isoformat(),
            "since": since.isoformat() if since else None,
        }
        return snapshot

    # ── Internal helpers ──────────────────────────────────────────────────────
    def _distribution(self, column, since: Optional[datetime] =None) -> dict[str, int]:
        """
        Generic GROUP BY counter for categorical columns.
        NULL values are excluded; key is the string value of the column.
        """
        with SessionLocal() as session:
            q = (
                select(column, func.count().label("cnt"))
                .where(column.isnot(None))
                .group_by(column)
            )
            if since:
                q = q.where(AuditLog.timestamp >= since)
            rows = session.execute(q).all() 
        return {str(row[0]): int(row[1]) for row in rows}           